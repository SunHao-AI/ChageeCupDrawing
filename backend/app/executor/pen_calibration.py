"""绘制前笔宽标定：扫描小程序粗细滑杆档位，实测各档落笔宽度，然后清空画布。

流程（全自动，无需人工介入）：
  1. 依次点击滑杆等分档位（滑杆为线性行程，档位由校准页框选的轨道区域等分）
  2. 每档在画布顶部「刻度带」画一条水平测试线，截图测出实际墨迹宽度
  3. 宽度单调不减地归并成 K 档宽度表；若所有档位测出的宽度几乎相同（小程序
     不支持调宽或滑杆标定不准），判定自动变粗不可用，回退单细笔
  4. 用校准页标定的撤销按钮连撤测试线笔数清空画布，截图验证已清空；无法清空
     （未标定撤销位置）则终止任务，避免在脏画布上绘制

输出 WidthTable：tier -> (滑杆位置 0..1, 实测宽度 px)，供执行器分档绘制时换笔。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

from app.executor.ink_measure import ink_occupied_fraction, measure_ink_width


class CalibrationError(Exception):
    """标定失败；code 供上层映射为稳定错误码。"""

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
class WidthTable:
    # tier 索引 -> (滑杆归一化位置, 实测笔宽 px)。按宽度从小到大。
    tiers: tuple[tuple[float, float], ...]

    @property
    def count(self) -> int:
        return len(self.tiers)


@dataclass(frozen=True)
class CalibrationResult:
    table: WidthTable
    strokes_drawn: int
    cleared: bool
    width_spread: float

class PenCalibrator:
    """在真机画布上扫描滑杆档位并实测笔宽。

    ``on_progress`` 回调用于向前端推 task.warning 进度消息。
    ``sleep`` 可注入以便测试免等待。
    """

    MAX_PROBE_STEPS = 14
    # 各档实测宽度差异小于该比例时认为滑杆无效（小程序不支持调宽）。
    MIN_SPREAD_RATIO = 0.18
    # 清空判定只看刻度带区域（测试线实际画在哪就查哪）：按整幅画布算占比时，
    # 几行测试线只占 ~0.5%，撤销失败也会被误判成「已清空」。
    CLEAR_CONFIRM_OCCUPIED = 0.003
    # 刻度带线长与测试区外边距（截图 px）。
    TEST_LINE_LEN = 46
    TEST_MARGIN = 6

    def __init__(
        self,
        *,
        probe: SliderProbe,
        mouse: object,
        to_screen: Callable[[float, float], tuple[float, float]],
        sleep: Callable[[float], None] = time.sleep,
        settle_s: float = 0.28,
    ) -> None:
        self._probe = probe
        self._mouse = mouse
        self._to_screen = to_screen
        self._sleep = sleep
        self._settle_s = float(settle_s)

    # ---------- 对外主流程 ----------

    def run(
        self,
        *,
        slider_track: dict,
        undo_point: dict | None,
        tiers: int,
        canvas: tuple[int, int, int, int],
        on_progress: Callable[[str], None],
        should_stop: Callable[[], bool],
    ) -> CalibrationResult:
        track = self._validate_track(slider_track)
        tier_count = max(2, min(int(tiers), self.MAX_PROBE_STEPS))

        rows, band = self._scan_widths(
            track=track, probe_steps=tier_count, canvas=canvas,
            on_progress=on_progress, should_stop=should_stop,
        )
        table, spread = self._build_table(rows, tier_count)

        # 无论调宽是否有效都要清空刻度带测试线，画布干净才算标定完成。
        cleared = self._clear_canvas(
            undo_point=undo_point, strokes=len(rows),
            canvas=canvas, band=band, on_progress=on_progress, should_stop=should_stop,
        )
        if not cleared:
            raise CalibrationError(
                "E_CALIBRATION_NOT_CLEARED",
                "标定测试线无法自动清空（请在校准页框选撤销按钮位置，或手动清空画布后关闭自动变粗）",
            )
        if table is None:
            # 所有档位宽度基本一致：调宽无效，回退单细笔（不算错误）。
            return CalibrationResult(
                table=WidthTable(((0.0, rows[0][1]),)),
                strokes_drawn=len(rows),
                cleared=True,
                width_spread=spread,
            )
        return CalibrationResult(table=table, strokes_drawn=len(rows), cleared=True, width_spread=spread)

    # ---------- 档位扫描 ----------

    def _scan_widths(
        self,
        *,
        track: tuple[int, int, int, int],
        probe_steps: int,
        canvas: tuple[int, int, int, int],
        on_progress: Callable[[str], None],
        should_stop: Callable[[], bool],
    ) -> tuple[list[tuple[float, float]], tuple[int, int, int, int]]:
        """逐档画测试线并实测宽度。

        返回 ([(滑杆位置, 宽度)], 刻度带外接框)：外接框供清空校验使用。
        """
        x, y, w, h = track
        mid_y = y + h / 2.0
        # 测试线放在画布顶部刻度带。行距按「历史最大宽度」增大（滑杆宽度非单调，
        # 只看上一行会让后面的宽线窜进上一行测宽窗）；测宽窗口从上一根线的
        # 下缘起算，上下两根线各占各的窗口；纵向超出画布后换列继续。
        canvas_x, canvas_y, canvas_w, canvas_h = canvas
        band_y0 = canvas_y + self.TEST_MARGIN + 4
        results: list[tuple[float, float]] = []
        band_y = band_y0
        col = 0
        max_w = 0.0
        prev_w = 0.0
        prev_band_y = band_y0
        band_x0 = canvas_x + self.TEST_MARGIN
        band_top = band_y0
        band_right = band_x0 + self.TEST_LINE_LEN
        band_bottom = band_y0
        for step in range(probe_steps):
            if should_stop():
                raise CalibrationError("E_STOPPED", "标定被停止")
            frac = step / max(1, probe_steps - 1)
            sx = x + w * frac
            self._ensure_foreground()
            self._click(sx, mid_y)
            self._sleep(self._settle_s)

            # 行距按历史最大宽度增大：滑杆宽度非单调（实测有抖动），只看上一行
            # 会让后面的宽线窜进上一行的测宽窗甚至粘连。
            if step > 0:
                prev_band_y = band_y
                band_y += max(14.0, max_w + 12.0)
            if band_y + max(max_w, 16.0) > canvas_y + canvas_h - self.TEST_MARGIN:
                col += 1
                band_y = band_y0
            line_x = canvas_x + self.TEST_MARGIN + col * (self.TEST_LINE_LEN + 14)
            begin = (line_x, band_y)
            end = (begin[0] + self.TEST_LINE_LEN, band_y)
            self._ensure_foreground()
            self._stroke(begin, end)
            self._sleep(0.12)

            image = self._grab()
            # 上边界压在上根线墨迹下缘之下：即便粗线窜上来也进不了本行测宽窗。
            if step == 0:
                top = band_y - 16.0
            else:
                top = prev_band_y + prev_w / 2.0 + 2.0
            bottom = band_y + max(24.0, prev_w) + 8.0
            region = (int(begin[0]) - 3, int(top), self.TEST_LINE_LEN + 10,
                      int(bottom - top))
            width = measure_ink_width(image, region)
            if width is None:
                # 该档没落上笔：可能滑杆点击触发了别的东西，等待后重试一次。
                self._click(sx, mid_y)
                self._sleep(self._settle_s)
                self._ensure_foreground()
                self._stroke(begin, end)
                self._sleep(0.12)
                image = self._grab()
                width = measure_ink_width(image, region)
            if width is None:
                raise CalibrationError(
                    "E_CALIBRATION_NO_INK",
                    "标定测试线未出现在画布上（检查滑杆/画布区域框选是否正确）",
                )
            prev_w = width
            max_w = max(max_w, width)
            results.append((frac, width))
            band_right = max(band_right, end[0] + 4)
            band_bottom = max(band_bottom, band_y + width / 2.0 + 4)
            band_top = min(band_top, band_y - width / 2.0 - 4)
            on_progress(f"笔宽标定 {step + 1}/{probe_steps}：实测 {width:.1f}px")
        band = (int(band_x0), int(band_top), int(band_right - band_x0),
                int(band_bottom - band_top))
        return results, band

    # ---------- 宽度表构建 ----------

    def _build_table(
        self, rows: Sequence[tuple[float, float]], tier_count: int
    ) -> tuple[WidthTable | None, float]:
        widths = [w for _, w in rows]
        spread = (max(widths) - min(widths)) / max(1e-6, max(widths))
        if spread < self.MIN_SPREAD_RATIO:
            return None, spread

        # 以最大宽度为最粗档：tier_k 取「实测宽度 <= 目标宽度」的最大探测位置，
        # 保证每档实际不会画得比计划粗；目标低于最细探测档时复用最细档。
        tiers: list[tuple[float, float]] = []
        for k in range(tier_count):
            target = max(widths) * (k + 1) / tier_count
            frac, width = rows[0]
            for row_frac, row_width in rows:
                if row_width <= target * 1.0001:
                    frac, width = row_frac, row_width
            if tiers and width <= tiers[-1][1] + 1e-9:
                frac, width = tiers[-1]
            tiers.append((frac, width))
        return WidthTable(tuple(tiers)), spread

    # ---------- 画布清空 ----------

    def _clear_canvas(
        self,
        *,
        undo_point: dict | None,
        strokes: int,
        canvas: tuple[int, int, int, int],
        band: tuple[int, int, int, int],
        on_progress: Callable[[str], None],
        should_stop: Callable[[], bool],
    ) -> bool:
        # 校验区域取「画布∩刻度带」：整幅画布的墨占比对几行测试线不敏感
        # （~0.5% < 旧阈值），撤销失败会被误判成已清空、直接开画留下脏线。
        check = band
        if not undo_point:
            image = self._grab()
            return ink_occupied_fraction(image, check) <= self.CLEAR_CONFIRM_OCCUPIED
        on_progress(f"清空标定测试线（撤销 {strokes} 笔）")
        for _ in range(strokes * 2 + 4):
            if should_stop():
                raise CalibrationError("E_STOPPED", "标定被停止")
            self._ensure_foreground()
            self._click(undo_point["x"], undo_point["y"])
            self._sleep(0.22)
            image = self._grab()
            if ink_occupied_fraction(image, check) <= self.CLEAR_CONFIRM_OCCUPIED:
                return True
        return False

    # ---------- 底层动作（可由子类替换以便测试） ----------

    def _ensure_foreground(self) -> None:
        if not self._probe.foreground_matches():
            self._probe.activate()
            self._sleep(0.15)

    def _click(self, x: float, y: float) -> None:
        # x/y 为截图坐标；鼠标动作前换算到屏幕坐标。
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
            raise CalibrationError("E_CALIBRATION_CAPTURE", "标定截图失败")
        return image

    @staticmethod
    def _validate_track(track: dict) -> tuple[int, int, int, int]:
        x, y = int(track.get("x", -1)), int(track.get("y", -1))
        w, h = int(track.get("w", 0)), int(track.get("h", 0))
        if x < 0 or y < 0 or w < 10 or h < 2:
            raise CalibrationError(
                "E_SLIDER_RECT", "滑杆区域未标定或过小（请在校准页框选粗细滑杆轨道）"
            )
        return x, y, w, h
