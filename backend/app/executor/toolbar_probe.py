"""工具栏行为探测：不依赖图标模板，靠「点击→观测」学出控件位置。

顺序：折叠 → 滑杆 → 撤销 → 橡皮/画笔。折叠先于滑杆（折叠态轨道不可见），
撤销先于橡皮/画笔（清理依赖撤销）。所有点击落在画布外，墨迹观测只看刻度带。
画笔「选中/未选中」两态背景亮度差即选中指纹，供绘制期校验当前工具态。
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
    """面板展开态下的工具位图；亮度指纹供绘制期判断当前是否画笔。"""

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
        fold = manual.fold if manual else None
        slider = manual.slider if manual else None
        undo = manual.undo if manual else None
        eraser = manual.eraser if manual else None
        brush = manual.brush if manual else None
        selected_lum = manual.brush_selected_lum if manual else None
        idle_lum = manual.brush_idle_lum if manual else None

        def check_stop() -> None:
            if should_stop():
                raise ProbeError("E_STOPPED", "工具栏探测被停止")

        check_stop()
        self._candidates = self._make_candidates(self._grab().shape)

        if fold is None:
            fold = self._probe_fold(check_stop)
            if fold is not None:
                on_progress("已识别 折叠按钮")

        if slider is None:
            self._ensure_panel_open(fold, check_stop)
            slider = self._find_slider(self._grab())
            if slider is None:
                if fold is None:
                    raise ProbeError(
                        "E_PROBE_NO_TOOLBAR", "工具条区域未探到任何控件（请在校准页人工补标）"
                    )
                raise ProbeError(
                    "E_PROBE_NO_SLIDER", "面板展开态下未找到笔宽滑杆轨道（请人工框选滑杆）"
                )
            on_progress("已识别 滑杆轨道")

        if undo is None:
            undo = self._probe_undo(fold, slider, check_stop)
            if undo is not None:
                on_progress("已识别 撤销按钮")

        if eraser is None or brush is None:
            self._ensure_panel_open(fold, check_stop)
            got_eraser, got_brush = self._probe_tools(fold, slider, undo, check_stop)
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
            selected_lum, idle_lum = self._fingerprint(brush, eraser, fold, check_stop)

        return ToolBitmap(
            brush=brush,
            eraser=eraser,
            slider=dict(slider) if slider is not None else None,
            undo=undo,
            fold=fold,
            brush_selected_lum=selected_lum,
            brush_idle_lum=idle_lum,
        )

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
    def _inside(rect: tuple[int, int, int, int], point: tuple[int, int]) -> bool:
        x0, y0, w, h = rect
        x, y = point
        return x0 <= x <= x0 + w and y0 <= y <= y0 + h

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
        """已识别控件（以及滑杆矩形内）的候选点不再点击，避免干扰状态。"""
        points: set[tuple[int, int]] = set()
        for item in (fold, undo, eraser, brush):
            if item is not None:
                points.add((int(item[0]), int(item[1])))
        if slider is not None:
            rect = (slider["x"], slider["y"], slider["w"], slider["h"])
            for point in self._candidates:
                if self._inside(rect, point):
                    points.add(point)
        return points

    # ---------- 1) 折叠按钮 ----------

    def _probe_fold(self, check_stop: Callable[[], None]) -> tuple[float, float] | None:
        """候选点逐个 click，滑杆轨道可见性翻转者即折叠钮；再点一次确认可逆。"""
        before_visible = self._find_slider(self._grab()) is not None
        for point in self._candidates:
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
            # 不可逆：尽量点回原态后放弃该候选。
            if restored_visible != before_visible:
                self._click(*point)
                self._sleep(self._settle_s)
            return None
        return None

    def _ensure_panel_open(
        self, fold: tuple[float, float] | None, check_stop: Callable[[], None]
    ) -> None:
        if self._find_slider(self._grab()) is not None:
            return
        if fold is None:
            return
        check_stop()
        self._click(float(fold[0]), float(fold[1]))
        self._sleep(self._settle_s)

    # ---------- 3) 撤销按钮 ----------

    def _probe_undo(
        self,
        fold: tuple[float, float] | None,
        slider: dict | None,
        check_stop: Callable[[], None],
    ) -> tuple[float, float] | None:
        """先画基线，再逐候选点击；带内墨量降到基线 40% 以下者即撤销。"""
        skip = self._skip_points(fold=fold, slider=slider)
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
                return (float(point[0]), float(point[1]))
            if occupied < baseline * 0.9:
                # 线被部分擦掉或意外消失：补描后才能继续测下一候选。
                self._draw_band_line()
        return None

    def _ensure_drawing_tool(
        self, skip: set[tuple[int, int]], check_stop: Callable[[], None]
    ) -> bool:
        """当前若非画笔态（试画无墨），扫描候选直到切到能落墨的工具。"""
        self._draw_band_line()
        if self._band_has_ink():
            return True
        for point in self._candidates:
            if point in skip:
                continue
            check_stop()
            self._click(*point)
            self._sleep(self._settle_s)
            self._draw_band_line()
            if self._band_has_ink():
                return True
        return False

    # ---------- 4) 橡皮 / 画笔 ----------

    def _probe_tools(
        self,
        fold: tuple[float, float] | None,
        slider: dict | None,
        undo: tuple[float, float] | None,
        check_stop: Callable[[], None],
    ) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
        """压线重描：线白化=橡皮；线仍在（新出墨）=画笔。

        先按当前可见墨量决定先找哪个：有墨说明当前是绘制工具，先找橡皮；
        无墨说明当前是橡皮，先找画笔。两者都靠「点击后重描」的可观测差异。
        """
        skip = self._skip_points(fold=fold, slider=slider, undo=undo)
        self._draw_band_line()
        has_ink = self._band_has_ink()
        eraser: tuple[float, float] | None = None
        brush: tuple[float, float] | None = None
        if has_ink:
            eraser = self._scan_tool(skip, check_stop, want_ink=False)
            if eraser is not None:
                skip.add((int(eraser[0]), int(eraser[1])))
            brush = self._scan_tool(skip, check_stop, want_ink=True)
        else:
            brush = self._scan_tool(skip, check_stop, want_ink=True)
            if brush is not None:
                skip.add((int(brush[0]), int(brush[1])))
            eraser = self._scan_tool(skip, check_stop, want_ink=False)
        return eraser, brush

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
            self._click(*point)
            self._sleep(self._settle_s)
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
        check_stop: Callable[[], None],
    ) -> tuple[float | None, float | None]:
        """橡皮选中态截画笔格 = idle；点画笔切回后截 = selected。"""
        self._ensure_panel_open(fold, check_stop)
        check_stop()
        self._click(float(eraser[0]), float(eraser[1]))
        self._sleep(self._settle_s)
        idle = self._cell_lum(brush)
        check_stop()
        self._click(float(brush[0]), float(brush[1]))
        self._sleep(self._settle_s)
        selected = self._cell_lum(brush)
        if idle is None or selected is None:
            return None, None
        if abs(selected - idle) < self.LUM_DELTA_MIN:
            return None, None
        return float(selected), float(idle)

    def _cell_lum(self, point: tuple[float, float]) -> float | None:
        image = self._grab()
        gray = image[..., 0] if image.ndim == 3 else image
        x, y = int(point[0]), int(point[1])
        crop = gray[max(0, y - 4) : y + 5, max(0, x - 4) : x + 5]
        if crop.size == 0:
            return None
        return float(crop.mean())

    # ---------- 刻度带 ----------

    def _draw_band_line(self) -> None:
        bx = self._band[0] + 8
        by = self._band[1] + self._band[3] // 2
        self._ensure_foreground()
        self._stroke((bx, by), (bx + self.BAND_LINE_LEN, by))
        self._sleep(0.12)

    def _band_occupied(self) -> float:
        return ink_occupied_fraction(self._grab(), self._band)

    def _band_has_ink(self) -> bool:
        return measure_ink_width(self._grab(), self._band) is not None

    def _band_eps(self) -> float:
        """线存在判据：至少半个线长对应的墨面积，避免大刻度带被稀释。"""
        _, _, w, h = self._band
        return max(1e-4, 0.5 * self.BAND_LINE_LEN / max(1, w * h))

    # ---------- 底层动作（可由子类替换以便测试） ----------

    def _ensure_foreground(self) -> None:
        if not self._probe.foreground_matches():
            self._probe.activate()
            self._sleep(0.15)

    def _click(self, x: float, y: float) -> None:
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
