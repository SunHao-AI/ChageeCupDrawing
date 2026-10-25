"""工具栏行为探测：不依赖图标模板，靠「点击→观测」学出控件位置。

顺序：折叠 → 滑杆 → 撤销 → 橡皮/画笔。折叠先于滑杆（折叠态轨道不可见），
撤销先于橡皮/画笔（清理依赖撤销）。所有点击落在画布外，墨迹观测只看刻度带。
画笔「选中/未选中」两态背景亮度差即选中指纹，供绘制期校验当前工具态。

人工兜底字段优先级最高：非 None 的人工值直接采用；但落在画布内的人工点会被
丢弃并改走自动探测，避免任何探测点击污染画布。探测期间在刻度带画的测试笔迹
在返回前会用已识别的撤销按钮清空，并把工具态还原为画笔。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable, Protocol

import numpy as np

from app.executor.ink_measure import ink_occupied_fraction, measure_ink_width


class ProbeError(Exception):
    """探测失败；code 供上层映射为稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SliderProbe(Protocol):
    """DrawingTarget 的图像视角适配：capture_image 直接给截图 ndarray。"""

    def capture_image(self): ...
    def foreground_matches(self) -> bool: ...
    def activate(self) -> bool: ...


@dataclass(frozen=True)
class ToolBitmap:
    """面板展开态下的工具位图；亮度指纹供绘制期判断当前是否画笔。

    人工兜底时，非 None 的字段优先于自动探测结果；只有缺失的亮度项才会被
    指纹流程重算，已给的亮度值始终保留。

    亮度指纹只有「brush_selected_lum 与 brush_idle_lum 两个值都存在」时才算
    完整指纹，才能参与选中态阈值比较；仅一侧有值时是部分指纹（例如人工只给
    了 selected、计算出的 idle 因亮度差不足被置 None），只能用于控件坐标，
    不得把 None 代入 (selected + idle) / 2 之类的亮度判定。
    """

    brush: tuple[float, float] | None
    eraser: tuple[float, float] | None
    slider: dict | None  # {"x","y","w","h"}
    undo: tuple[float, float] | None
    fold: tuple[float, float] | None
    brush_selected_lum: float | None
    brush_idle_lum: float | None


class ToolbarProber:
    """在画布下方工具条上做行为探测，产出各控件位置与画笔选中指纹。"""

    BAND_LINE_LEN = 46
    LUM_DELTA_MIN = 30  # 选中态判定最小亮度差
    CANDIDATE_STEP = 28  # 工具条候选点网格步长
    # 折叠钮扫描的候选行预算。候选按行优先、距画布由近到远，截断等价于「只扫
    # 画布下方最近几行」，与截图宽度无关；按点数限死会让宽屏只覆盖不到两行。
    # 4 行 ≈ 画布下缘 112px：工具条通常紧贴画布下方 70px 左右，3 行会漏掉折叠钮。
    # 更远的折叠钮不再自动探测（用户可在校准页人工补标），避免无工具条应用在
    # 首绘承担全网格盲点的分钟级 worst case。
    MAX_FOLD_ROWS = 4
    # 撤销判据：刻度带内墨量降到基线该比例以下即认定撤销生效。
    UNDO_DROP_RATIO = 0.4
    INK_THRESHOLD = 160
    # 滑杆轨道的几何特征：连续暗段最小宽度、允许的行带高度。
    SLIDER_MIN_RUN = 60
    SLIDER_MAX_HEIGHT = 30
    SLIDER_MIN_HEIGHT = 2

    def __init__(
        self,
        *,
        probe: SliderProbe,
        mouse: object,
        to_screen: Callable[[float, float], tuple[float, float]],
        sleep: Callable[[float], None] = time.sleep,
        settle_s: float = 0.45,
    ) -> None:
        self._probe = probe
        self._mouse = mouse
        self._to_screen = to_screen
        self._sleep = sleep
        self._settle_s = float(settle_s)
        self._canvas: tuple[int, int, int, int] = (0, 0, 0, 0)
        self._band: tuple[int, int, int, int] = (0, 0, 0, 0)
        self._candidates: list[tuple[int, int]] = []
        # 折叠扫描的候选点数上限，由 run() 按截图宽度与 MAX_FOLD_ROWS 计算。
        self._fold_limit = 0
        # 探测前的刻度带墨像素数：清理只需回到这个深度，绝不越过它去撤用户笔画。
        self._band_baseline_px = 0
        # 探测自己画出且真正改变像素的笔数（与撤销历史一一对应），以及已消耗的撤销次数。
        self._probe_strokes = 0
        self._probe_undos = 0
        # 正常路径清理成功后置 True；异常/停止路径的 finally 据此避免重复撤销。
        self._ink_cleared = False

    # ---------- 对外主流程 ----------

    def run(
        self,
        *,
        canvas: tuple[int, int, int, int],
        band: tuple[int, int, int, int],
        open_bitmap: "ToolBitmap | None",
        on_progress: Callable[[str], None],
        should_stop: Callable[[], bool],
    ) -> ToolBitmap:
        self._canvas = tuple(int(v) for v in canvas)
        self._band = tuple(int(v) for v in band)

        manual = open_bitmap
        fold = self._clean_point(manual.fold if manual else None)
        slider = self._clean_slider(manual.slider if manual else None)
        undo = self._clean_point(manual.undo if manual else None)
        eraser = self._clean_point(manual.eraser if manual else None)
        brush = self._clean_point(manual.brush if manual else None)
        selected_lum = manual.brush_selected_lum if manual else None
        idle_lum = manual.brush_idle_lum if manual else None
        slider_manual = slider is not None

        def check_stop() -> None:
            if should_stop():
                raise ProbeError("E_STOPPED", "工具栏探测被停止")

        check_stop()
        first_image = self._grab()
        self._candidates = self._make_candidates(first_image.shape)
        cols = max(1, math.ceil(first_image.shape[1] / self.CANDIDATE_STEP))
        self._fold_limit = min(len(self._candidates), self.MAX_FOLD_ROWS * cols)
        self._band_baseline_px = self._band_ink_pixels(first_image)
        self._probe_strokes = 0
        self._probe_undos = 0
        self._ink_cleared = False

        if fold is None:
            skip = self._skip_points(slider=slider, undo=undo, eraser=eraser, brush=brush)
            fold = self._probe_fold(skip, check_stop)
            if fold is not None:
                on_progress("已识别 折叠按钮")

        if slider is None:
            if not self._ensure_panel_open(fold, check_stop):
                if fold is None:
                    raise ProbeError(
                        "E_PROBE_NO_TOOLBAR", "工具条区域未探到任何控件（请在校准页人工补标）"
                    )
                raise ProbeError(
                    "E_PROBE_NO_SLIDER", "面板展开态下未找到笔宽滑杆轨道（请人工框选滑杆）"
                )
            slider = self._find_slider(self._grab())
            if slider is None:
                raise ProbeError(
                    "E_PROBE_NO_SLIDER", "面板展开态下未找到笔宽滑杆轨道（请人工框选滑杆）"
                )
            on_progress("已识别 滑杆轨道")

        if undo is None:
            undo = self._probe_undo(fold, slider, eraser, brush, check_stop)
            if undo is not None:
                on_progress("已识别 撤销按钮")

        tools_probed = eraser is None or brush is None
        if tools_probed:
            self._require_panel_open(fold, check_stop, assume_open=slider_manual)
            got_eraser, got_brush = self._probe_tools(
                fold, slider, undo, eraser, brush, check_stop
            )
            if eraser is None and got_eraser is not None:
                eraser = got_eraser
                on_progress("已识别 橡皮按钮")
            if brush is None and got_brush is not None:
                brush = got_brush
                on_progress("已识别 画笔按钮")

        if (
            brush is not None
            and eraser is not None
            and (selected_lum is None or idle_lum is None)
        ):
            selected_lum, idle_lum = self._fingerprint(
                brush, eraser, fold, selected_lum, idle_lum, check_stop,
                assume_open=slider_manual,
            )

        # 清理探测笔迹：残留墨会落进 pen_calibration 的首个测宽窗口。
        try:
            self._require_panel_open(fold, check_stop, assume_open=slider_manual)
            self._clear_probe_ink(undo, check_stop)
            self._ink_cleared = True
            if tools_probed and brush is not None:
                check_stop()
            return ToolBitmap(
                brush=brush,
                eraser=eraser,
                slider=dict(slider) if slider is not None else None,
                undo=undo,
                fold=fold,
                brush_selected_lum=selected_lum,
                brush_idle_lum=idle_lum,
            )
        finally:
            # 探测中途失败/被停止时同样要清掉刻度带测试线，否则残留墨会污染
            # 下次重扫与标定；清理失败不得掩盖正在传播的原始异常。
            self._best_effort_cleanup(undo, fold, slider, eraser, brush)
            # 探测中可能停在橡皮态；即便清理/展开抛错也要尽力切回画笔，否则上层
            # 探测失败时仍跳过画笔校验，后续绘制会擦掉用户画作。
            if tools_probed and brush is not None:
                self._restore_brush(brush, fold)

    def _best_effort_cleanup(
        self,
        undo: tuple[float, float] | None,
        fold: tuple[float, float] | None,
        slider: dict | None,
        eraser: tuple[float, float] | None,
        brush: tuple[float, float] | None,
    ) -> None:
        """异常/停止路径尽力清理探测笔迹，正常路径已清理则不重复。

        清理不得因停止信号中断（no-op check_stop），失败也只吞掉自身异常，让
        原始异常继续向外传播。``eraser``/``brush`` 仅作探测已识别控件的记录。
        """
        if self._ink_cleared:
            return
        try:
            # 撤销按钮只在展开态可见；人工滑杆等价于用户保证展开态坐标可信。
            if not self._ensure_panel_open(fold, lambda: None):
                if slider is None:
                    return
            self._clear_probe_ink(undo, lambda: None)
            self._ink_cleared = True
        except Exception:  # 清理路径：任何失败都不得掩盖原始异常
            pass

    def _restore_brush(
        self, brush: tuple[float, float], fold: tuple[float, float] | None
    ) -> None:
        """异常路径恢复画笔；失败不得掩盖正在传播的原始异常。"""
        try:
            # 用 no-op check_stop：恢复是收尾动作，停止信号不应在此再抛。
            if not self._ensure_panel_open(fold, lambda: None):
                return
            self._click(float(brush[0]), float(brush[1]))
            self._sleep(self._settle_s)
        except Exception:  # 清理路径：任何恢复失败都不得掩盖原始异常
            pass

    # ---------- 人工兜底校验 ----------

    def _clean_point(self, point) -> tuple[float, float] | None:
        """人工点非法（结构错误）抛稳定错误码；落在画布内则丢弃改自动探测。"""
        if point is None:
            return None
        try:
            if isinstance(point, dict):
                x, y = float(point["x"]), float(point["y"])
            else:
                x, y = float(point[0]), float(point[1])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProbeError(
                "E_PROBE_BAD_MANUAL", "人工控件坐标结构非法（需 (x, y) 或 {x, y}）"
            ) from exc
        if self._inside(self._canvas, (x, y)):
            return None
        return (x, y)

    def _clean_slider(self, slider) -> dict | None:
        """人工滑杆校验：结构非法抛 E_PROBE_BAD_MANUAL；与画布相交则丢弃。"""
        if slider is None:
            return None
        if not isinstance(slider, dict):
            raise ProbeError("E_PROBE_BAD_MANUAL", "人工滑杆坐标结构非法（需 x/y/w/h）")
        try:
            rect = {key: int(slider[key]) for key in ("x", "y", "w", "h")}
        except (KeyError, TypeError, ValueError) as exc:
            raise ProbeError(
                "E_PROBE_BAD_MANUAL", "人工滑杆坐标结构非法（需 x/y/w/h 整数值）"
            ) from exc
        if rect["w"] <= 0 or rect["h"] <= 0:
            raise ProbeError("E_PROBE_BAD_MANUAL", "人工滑杆尺寸非法（w/h 必须为正）")
        if self._rect_overlaps(rect, self._canvas):
            return None
        return rect

    # ---------- 候选点与工具条几何 ----------

    def _make_candidates(self, shape: tuple[int, ...]) -> list[tuple[int, int]]:
        """画布下缘 +8 到截图底缘之间的网格点；跳过画布与刻度带内部。"""
        height, width = shape[0], shape[1]
        canvas = self._canvas
        y_start = canvas[1] + canvas[3] + 8
        if y_start >= height:
            return []
        points: list[tuple[int, int]] = []
        for y in range(y_start, height, self.CANDIDATE_STEP):
            for x in range(self.CANDIDATE_STEP // 2, width, self.CANDIDATE_STEP):
                point = (int(x), int(y))
                if self._inside(canvas, point) or self._inside(self._band, point):
                    continue
                points.append(point)
        return points

    @staticmethod
    def _inside(rect: tuple[int, int, int, int], point: tuple[float, float]) -> bool:
        x0, y0, w, h = rect
        x, y = point
        return x0 <= x <= x0 + w and y0 <= y <= y0 + h

    @staticmethod
    def _rect_overlaps(a: dict, b: tuple[int, int, int, int]) -> bool:
        ax, ay, aw, ah = a["x"], a["y"], a["w"], a["h"]
        bx, by, bw, bh = b
        return not (ax + aw < bx or bx + bw < ax or ay + ah < by or by + bh < ay)

    def _find_slider(self, image) -> dict | None:
        """工具条区域水平暗段行投影：连续暗 run > 60px、行带高 2~30px 即滑杆轨道。"""
        gray = image[..., 0] if image.ndim == 3 else image
        height = gray.shape[0]
        top = min(height, self._canvas[1] + self._canvas[3] + 8)
        if top >= height:
            return None
        dark = gray[top:height, :] < self.INK_THRESHOLD

        groups: list[list[int]] = []
        current: list[int] = []
        for row in range(dark.shape[0]):
            if self._longest_run(dark[row]) > self.SLIDER_MIN_RUN:
                current.append(row)
            elif current:
                groups.append(current)
                current = []
        if current:
            groups.append(current)

        best: dict | None = None
        for group in groups:
            if not (self.SLIDER_MIN_HEIGHT <= len(group) <= self.SLIDER_MAX_HEIGHT):
                continue
            rows = dark[group[0] : group[-1] + 1]
            cols = np.flatnonzero(rows.any(axis=0))
            if cols.size == 0:
                continue
            x0, x1 = int(cols[0]), int(cols[-1]) + 1
            rect = {"x": x0, "y": int(top + group[0]), "w": x1 - x0, "h": len(group)}
            if best is None or rect["w"] > best["w"]:
                best = rect
        return best

    @staticmethod
    def _longest_run(row: np.ndarray) -> int:
        padded = np.concatenate(
            (np.zeros(1, dtype=np.int8), row.astype(np.int8), np.zeros(1, dtype=np.int8))
        )
        diff = np.diff(padded)
        starts = np.flatnonzero(diff == 1)
        ends = np.flatnonzero(diff == -1)
        if starts.size == 0:
            return 0
        return int((ends - starts).max())

    def _skip_points(
        self,
        *,
        fold: tuple[float, float] | None = None,
        slider: dict | None = None,
        undo: tuple[float, float] | None = None,
        eraser: tuple[float, float] | None = None,
        brush: tuple[float, float] | None = None,
    ) -> set[tuple[int, int]]:
        """已识别/人工给定控件的邻域候选不再点击。

        图标按钮通常比网格步长大，只跳精确坐标会把同一按钮内的相邻候选再点一次，
        从而误翻转面板或误触工具；这里按 CANDIDATE_STEP 半径跳过整块按钮。
        """
        radius = float(self.CANDIDATE_STEP)
        points: set[tuple[int, int]] = set()
        for item in (fold, undo, eraser, brush):
            if item is None:
                continue
            px, py = float(item[0]), float(item[1])
            for point in self._candidates:
                if math.hypot(point[0] - px, point[1] - py) <= radius:
                    points.add(point)
        if slider is not None:
            rect = (slider["x"], slider["y"], slider["w"], slider["h"])
            for point in self._candidates:
                if self._inside(rect, point):
                    points.add(point)
        return points

    # ---------- 1) 折叠按钮 ----------

    def _probe_fold(
        self, skip: set[tuple[int, int]], check_stop: Callable[[], None]
    ) -> tuple[float, float] | None:
        """候选点逐个 click，滑杆轨道可见性翻转者即折叠钮；再点一次确认可逆。

        不可逆的候选（点了不恢复）只算干扰：标记后继续扫描，并以当前可见性
        作为新基线——折叠钮在两个方向上都会翻转可见性，后续仍能识别。

        只扫画布下方最近 MAX_FOLD_ROWS 行候选（行优先、由近到远）；更远的折叠钮
        放弃自动探测，请用户在校准页人工补标。
        """
        before_visible = self._find_slider(self._grab()) is not None
        blocked: set[tuple[int, int]] = set()
        for point in self._candidates[: self._fold_limit]:
            if point in skip or point in blocked:
                continue
            check_stop()
            self._click(*point)
            self._sleep(self._settle_s)
            after_visible = self._find_slider(self._grab()) is not None
            if after_visible == before_visible:
                continue
            # 翻转成功：再点一次应回到原态，防误点系统控件。
            self._click(*point)
            self._sleep(self._settle_s)
            restored_visible = self._find_slider(self._grab()) is not None
            if restored_visible == before_visible:
                return (float(point[0]), float(point[1]))
            # 不可逆：记为干扰点，改用当前可见性继续扫描后续候选。
            blocked.add(point)
            before_visible = restored_visible
        return None

    def _ensure_panel_open(
        self, fold: tuple[float, float] | None, check_stop: Callable[[], None]
    ) -> bool:
        if self._find_slider(self._grab()) is not None:
            return True
        if fold is None:
            return False
        check_stop()
        self._click(float(fold[0]), float(fold[1]))
        self._sleep(self._settle_s)
        return self._find_slider(self._grab()) is not None

    def _require_panel_open(
        self,
        fold: tuple[float, float] | None,
        check_stop: Callable[[], None],
        *,
        assume_open: bool = False,
    ) -> None:
        """工具坐标按展开态采集；无法确认展开时宁可报错也不要在错位坐标上乱点。"""
        if self._ensure_panel_open(fold, check_stop):
            return
        if assume_open:
            # 人工滑杆已给定：用户提供的就是展开态坐标，无法再靠轨道可见性判断。
            return
        raise ProbeError(
            "E_PROBE_NO_TOOLBAR", "面板处于折叠态且无折叠钮坐标，工具坐标不可用"
        )

    # ---------- 3) 撤销按钮 ----------

    def _probe_undo(
        self,
        fold: tuple[float, float] | None,
        slider: dict | None,
        eraser: tuple[float, float] | None,
        brush: tuple[float, float] | None,
        check_stop: Callable[[], None],
    ) -> tuple[float, float] | None:
        """先画基线，再逐候选点击；带内墨量降到基线 40% 以下者即撤销。"""
        skip = self._skip_points(
            fold=fold, slider=slider, eraser=eraser, brush=brush
        )
        if not self._ensure_drawing_tool(skip, check_stop):
            return None
        baseline = self._band_occupied()
        if baseline <= self._band_eps():
            return None
        for point in self._candidates:
            if point in skip:
                continue
            check_stop()
            self._click(*point)
            self._sleep(self._settle_s)
            occupied = self._band_occupied()
            if occupied < baseline * self.UNDO_DROP_RATIO:
                # 这一击已消耗掉一笔探测笔迹，清理时不再重复撤销。
                self._probe_undos += 1
                return (float(point[0]), float(point[1]))
            if occupied < baseline * 0.9:
                # 线被部分擦掉或意外消失：_ensure_drawing_tool 会切回绘制工具并
                # 重画一条线，不能再叠加一笔，否则撤销一次只掉一层、判据失真。
                if not self._ensure_drawing_tool(skip, check_stop):
                    return None
                baseline = max(baseline, self._band_occupied())
        return None

    def _ensure_drawing_tool(
        self, skip: set[tuple[int, int]], check_stop: Callable[[], None]
    ) -> bool:
        """当前若非画笔态（试画无墨），扫描候选直到切到能落墨的工具。"""
        if self._draw_band_line():
            return True
        for point in self._candidates:
            if point in skip:
                continue
            check_stop()
            before = self._band_ink_pixels()
            self._click(*point)
            self._sleep(self._settle_s)
            self._note_undo_like_drop(before)
            if self._draw_band_line():
                return True
        return False

    def _note_undo_like_drop(self, before: int) -> None:
        """候选点击若把带内墨量砍掉大半，说明它其实消耗了一笔探测笔迹。

        撤销按钮在探测早期尚未识别时可能被当作普通候选点到；漏记会让清理
        阶段的预算多出一笔，从而吃掉用户早先的画。
        """
        if before <= 0:
            return
        if self._band_ink_pixels() < before * self.UNDO_DROP_RATIO:
            self._probe_undos += 1

    # ---------- 4) 橡皮 / 画笔 ----------

    def _probe_tools(
        self,
        fold: tuple[float, float] | None,
        slider: dict | None,
        undo: tuple[float, float] | None,
        eraser: tuple[float, float] | None,
        brush: tuple[float, float] | None,
        check_stop: Callable[[], None],
    ) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
        """压线重描：线白化=橡皮；线仍在（新出墨）=画笔。

        先按当前可见墨量决定先找哪个：有墨说明当前是绘制工具，先找橡皮；
        无墨说明当前是橡皮，先找画笔。两者都靠「点击后重描」的可观测差异。
        """
        skip = self._skip_points(
            fold=fold, slider=slider, undo=undo, eraser=eraser, brush=brush
        )
        has_ink = self._draw_band_line()
        found_eraser: tuple[float, float] | None = None
        found_brush: tuple[float, float] | None = None
        if has_ink:
            found_eraser = self._scan_tool(skip, check_stop, want_ink=False)
            if found_eraser is not None:
                skip |= self._skip_points(eraser=found_eraser)
            found_brush = self._scan_tool(skip, check_stop, want_ink=True)
        else:
            found_brush = self._scan_tool(skip, check_stop, want_ink=True)
            if found_brush is not None:
                skip |= self._skip_points(brush=found_brush)
            found_eraser = self._scan_tool(skip, check_stop, want_ink=False)
        return found_eraser, found_brush

    def _scan_tool(
        self,
        skip: set[tuple[int, int]],
        check_stop: Callable[[], None],
        *,
        want_ink: bool,
    ) -> tuple[float, float] | None:
        for point in self._candidates:
            if point in skip:
                continue
            check_stop()
            before = self._band_ink_pixels()
            self._click(*point)
            self._sleep(self._settle_s)
            self._note_undo_like_drop(before)
            self._draw_band_line()
            if self._band_has_ink() == want_ink:
                return (float(point[0]), float(point[1]))
        return None

    # ---------- 5) 画笔选中指纹 ----------

    def _fingerprint(
        self,
        brush: tuple[float, float],
        eraser: tuple[float, float],
        fold: tuple[float, float] | None,
        selected_lum: float | None,
        idle_lum: float | None,
        check_stop: Callable[[], None],
        *,
        assume_open: bool = False,
    ) -> tuple[float | None, float | None]:
        """橡皮选中态截画笔格 = idle；点画笔切回后截 = selected。

        人工已给的一项不重算；亮度差不足只把「本次计算所得」置 None。
        流程总是以画笔选中态收尾。
        """
        self._require_panel_open(fold, check_stop, assume_open=assume_open)
        idle_missing = idle_lum is None
        selected_missing = selected_lum is None

        if idle_missing:
            check_stop()
            self._click(float(eraser[0]), float(eraser[1]))
            self._sleep(self._settle_s)
            idle_lum = self._cell_lum(brush)

        check_stop()
        self._click(float(brush[0]), float(brush[1]))
        self._sleep(self._settle_s)
        if selected_missing:
            selected_lum = self._cell_lum(brush)

        if idle_lum is None or selected_lum is None:
            return selected_lum, idle_lum
        if abs(selected_lum - idle_lum) < self.LUM_DELTA_MIN:
            if idle_missing:
                idle_lum = None
            if selected_missing:
                selected_lum = None
        return selected_lum, idle_lum

    def _cell_lum(self, point: tuple[float, float]) -> float | None:
        image = self._grab()
        gray = image[..., 0] if image.ndim == 3 else image
        x, y = int(point[0]), int(point[1])
        crop = gray[max(0, y - 4) : y + 5, max(0, x - 4) : x + 5]
        if crop.size == 0:
            return None
        return float(crop.mean())

    # ---------- 刻度带 ----------

    def _draw_band_line(self) -> bool:
        """在刻度带固定位置画一条测试线；返回该笔是否新增了墨。

        落笔位置必须固定：橡皮判定依赖「重描后整条线消失」。只在像素确实
        改变时计入探测笔数——没改变像素的试画（橡皮划过空白）不会留下撤销
        历史，也不该在清理阶段多撤一次。
        """
        bx = self._band[0] + 8
        by = self._band[1] + self._band[3] // 2
        before = self._band_ink_pixels()
        self._ensure_foreground()
        self._stroke((bx, by), (bx + self.BAND_LINE_LEN, by))
        self._sleep(0.12)
        after = self._band_ink_pixels()
        if after != before:
            self._probe_strokes += 1
        return after > before

    def _band_ink_pixels(self, image=None) -> int:
        gray = self._grab() if image is None else image
        gray = gray[..., 0] if gray.ndim == 3 else gray
        x0, y0, w, h = self._band
        region = gray[y0 : y0 + h, x0 : x0 + w]
        if region.size == 0:
            return 0
        return int((region < self.INK_THRESHOLD).sum())

    def _band_occupied(self) -> float:
        return ink_occupied_fraction(self._grab(), self._band)

    def _band_has_ink(self) -> bool:
        return measure_ink_width(self._grab(), self._band) is not None

    def _band_eps(self) -> float:
        """线存在判据：至少半个线长对应的墨面积，避免大刻度带被稀释。"""
        _, _, w, h = self._band
        return max(1e-4, 0.5 * self.BAND_LINE_LEN / max(1, w * h))

    def _clear_probe_ink(
        self, undo: tuple[float, float] | None, check_stop: Callable[[], None]
    ) -> None:
        """只撤销探测自己画出的笔数，撤到探测前的撤销历史深度为止。

        预算式清理：精确点击「已落笔数 - 已消耗撤销数」次，绝不要求「带空后
        再多点两次」，否则会越过探测起点、弹出用户早先画在画布别处的真实笔画。
        撤销历史里混有探测自己的橡皮笔迹，撤到它们时带内墨量会回升，不能用
        「单调下降」判据，只看最终刻度带是否回到基线墨量。
        """
        if self._probe_strokes <= 0:
            return
        tolerance = self._band_baseline_px + int(self._band_eps() * 0.5 + 1)
        if undo is None:
            if self._band_ink_pixels() <= tolerance:
                return
            raise ProbeError(
                "E_PROBE_UNCLEANED",
                "探测笔迹无法清理（未识别撤销按钮），请人工清空刻度带或补标撤销",
            )
        remaining = max(0, self._probe_strokes - self._probe_undos)
        for _ in range(remaining):
            check_stop()
            self._probe_undos += 1
            self._click(float(undo[0]), float(undo[1]))
            self._sleep(0.22)
        if self._band_ink_pixels() > tolerance:
            raise ProbeError(
                "E_PROBE_UNCLEANED", "探测笔迹未能撤销干净（撤销点击无效或笔迹过多）"
            )

    # ---------- 底层动作（可由子类替换以便测试） ----------

    def _ensure_foreground(self) -> None:
        if not self._probe.foreground_matches():
            self._probe.activate()
            self._sleep(0.15)

    def _click(self, x: float, y: float) -> None:
        # 画布内点击是硬约束：无论坐标来自候选网格还是人工兜底，都在这里兜底拒绝。
        if self._inside(self._canvas, (float(x), float(y))):
            raise ProbeError(
                "E_PROBE_CLICK_IN_CANVAS", "探测点击落在画布内，已拒绝执行"
            )
        self._ensure_foreground()
        move_to = getattr(self._mouse, "move_to")
        down = getattr(self._mouse, "down")
        up = getattr(self._mouse, "up")
        sx, sy = self._to_screen(x, y)
        move_to(sx, sy)
        self._sleep(0.01)
        down()
        self._sleep(0.022)
        up()
        self._sleep(0.02)

    def _stroke(self, begin: tuple[float, float], end: tuple[float, float]) -> None:
        move_to = getattr(self._mouse, "move_to")
        down = getattr(self._mouse, "down")
        up = getattr(self._mouse, "up")
        move_sample = getattr(self._mouse, "move_sample")
        bx, by = self._to_screen(*begin)
        ex, ey = self._to_screen(*end)
        move_to(bx, by)
        down()
        steps = max(6, int(math.dist((bx, by), (ex, ey))))
        for i in range(1, steps + 1):
            t = i / steps
            move_sample(bx + (ex - bx) * t, by + (ey - by) * t)
            self._sleep(0.004)
        up()

    def _grab(self):
        image = self._probe.capture_image()
        if image is None:
            raise ProbeError("E_PROBE_CAPTURE", "工具栏探测截图失败")
        return image
