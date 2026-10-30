"""ToolbarProber 仿真测试：合成工具栏按 panel_open/tool 状态渲染，点击按控件矩形派发状态迁移。

画板只模拟行为（面板展开/折叠、工具切换、撤销、滑杆），不模拟图标内容，
与 ToolbarProber「点击→观测变化」的探测判据一一对应。
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from app.executor.toolbar_probe import ProbeError, ToolBitmap, ToolbarProber

CANVAS = (40, 60, 300, 360)
BAND = (46, 70, 200, 40)
CANVAS_BOTTOM = CANVAS[1] + CANVAS[3]


class ToolbarBoard:
    """合成工具栏：grid 上画折叠钮/画笔格/橡皮格/撤销格；panel_open 决定
    滑杆是否可见与工具格亮度；handle_click 按控件矩形派发状态迁移。"""

    FOLD = (300, 500, 24, 24)
    BRUSH = (10, 495, 40, 40)
    ERASER = (60, 495, 40, 40)
    UNDO = (110, 495, 40, 40)
    SLIDER = (100, 530, 200, 12)
    SHIFT = 40  # 折叠态工具格整体上移量（折叠钮本身不动，以便再次点击切回）
    SELECTED_LUM = 180
    IDLE_LUM = 250

    def __init__(self) -> None:
        self.canvas_pixels = np.full((700, 600), 255, dtype=np.uint8)
        self.panel_open = True
        self.tool = "brush"
        self.stroke_width = 3.0
        self.strokes: list[list[tuple[int, int, int, int, np.ndarray]]] = []
        self.pending: list[tuple[int, int, int, int, np.ndarray]] = []
        self.pending_touched = False
        self.clicks: list[tuple[float, float]] = []
        self.undos = 0

    # ---------- 状态渲染 ----------

    def _tool_rect(self, rect):
        x, y, w, h = rect
        if not self.panel_open:
            y -= self.SHIFT
        return x, y, w, h

    def _hit(self, rect, x: float, y: float) -> bool:
        x0, y0, w, h = rect
        return x0 <= x <= x0 + w and y0 <= y <= y0 + h

    def image(self) -> np.ndarray:
        img = self.canvas_pixels.copy()
        if self.panel_open:
            x, y, w, h = self.SLIDER
            img[y : y + h, x : x + w] = 0
        cells = (
            ("fold", self.FOLD, 235),
            ("brush", self._tool_rect(self.BRUSH), None),
            ("eraser", self._tool_rect(self.ERASER), None),
            ("undo", self._tool_rect(self.UNDO), 240),
        )
        for name, (x, y, w, h), fixed in cells:
            if name == "brush":
                lum = self.SELECTED_LUM if self.tool == "brush" else self.IDLE_LUM
            elif name == "eraser":
                lum = self.SELECTED_LUM if self.tool == "eraser" else self.IDLE_LUM
            else:
                lum = fixed
            img[y : y + h, x : x + w] = lum
        return img

    # ---------- 鼠标动作派发 ----------

    def handle_click(self, x: float, y: float) -> None:
        if self._hit(self.FOLD, x, y):
            self.panel_open = not self.panel_open
            return
        if self._hit(self._tool_rect(self.UNDO), x, y):
            self.undo()
            return
        if self._hit(self._tool_rect(self.BRUSH), x, y):
            self.tool = "brush"
            return
        if self._hit(self._tool_rect(self.ERASER), x, y):
            self.tool = "eraser"
            return
        if self.panel_open and self._hit(self.SLIDER, x, y):
            frac = (x - self.SLIDER[0]) / self.SLIDER[2]
            self.stroke_width = 3.0 + 22.0 * max(0.0, min(1.0, frac))

    def apply_stroke(self, x0: float, y0: float, x1: float, y1: float) -> None:
        width = max(1, int(round(self.stroke_width)))
        half = width // 2
        xs0, xs1 = int(min(x0, x1)), int(max(x0, x1)) + 1
        ys0, ys1 = int(min(y0, y1)) - half, int(max(y0, y1)) + half + 1
        region = self.canvas_pixels[ys0:ys1, xs0:xs1]
        prev = region.copy()
        region[:] = 0 if self.tool == "brush" else 255
        self.pending.append((ys0, ys1, xs0, xs1, prev))
        if not np.array_equal(prev, region):
            self.pending_touched = True

    def undo(self) -> None:
        self.undos += 1
        if not self.strokes:
            return
        for ys0, ys1, xs0, xs1, prev in reversed(self.strokes.pop()):
            self.canvas_pixels[ys0:ys1, xs0:xs1] = prev

    def band_ink(self) -> float:
        return float((self.canvas_pixels[70:110, 46:246] < 160).mean())


class FoldInterferenceBoard(ToolbarBoard):
    """(70,512) 放一个只能把面板永久关掉的干扰控件；真实折叠钮仍在 FOLD 矩形。

    干扰点必须落在撤销预扫描安全区（x < 画布左缘 + 一个网格步长）之外，才
    不会被预扫描当成撤销候选点掉，仍走折叠钮盲扫路径。
    """

    INTERFERENCE = (70, 512)

    def __init__(self) -> None:
        super().__init__()
        self.interference_hits = 0

    def handle_click(self, x: float, y: float) -> None:
        if (
            abs(x - self.INTERFERENCE[0]) <= 2
            and abs(y - self.INTERFERENCE[1]) <= 2
            and self.interference_hits < 2
        ):
            self.interference_hits += 1
            self.panel_open = False
            return
        super().handle_click(x, y)


class DualButtonBoard(ToolbarBoard):
    """真实画板形态：「折叠」「展开」是两个独立按钮。

    折叠钮（FOLD 矩形）只在展开态生效；展开钮（UNFOLD_CELL 网格点位的矩形）
    只在收起态生效，各自不可原位互逆。
    """

    UNFOLD_CELL = (266, 456)
    UNFOLD_RECT = (256, 446, 20, 20)  # 只罩住 UNFOLD_CELL 这一个候选点（工具行右侧空白）

    def __init__(self) -> None:
        super().__init__()
        self.fold_hits = 0
        self.unfold_hits = 0

    def handle_click(self, x: float, y: float) -> None:
        if self._hit(self.FOLD, x, y):
            self.fold_hits += 1
            if self.panel_open:
                self.panel_open = False
            return
        if self._hit(self.UNFOLD_RECT, x, y):
            self.unfold_hits += 1
            if not self.panel_open:
                self.panel_open = True
            return
        super().handle_click(x, y)


class FoldOnlyBoard(ToolbarBoard):
    """折叠钮只能关不能开（没有展开钮）：成对复验必须否决该候选。"""

    def handle_click(self, x: float, y: float) -> None:
        if self._hit(self.FOLD, x, y):
            if self.panel_open:
                self.panel_open = False
            return
        super().handle_click(x, y)


class NoUndoBoard(ToolbarBoard):
    """撤销格被完全屏蔽：探测既找不到撤销，也无法清理测试笔迹。"""

    def handle_click(self, x: float, y: float) -> None:
        if self._hit(self._tool_rect(self.UNDO), x, y):
            return
        super().handle_click(x, y)


class SuppressingBoard(ToolbarBoard):
    """(70,512) 点击会切到橡皮并擦掉刻度带内约一半的现有墨迹。

    擦除量刻意落在「部分丢失」区间（基线 40%~90% 之间），以触发撤销探测的
    补描分支；擦除区域取自当前实际着墨列，确保与测试线真正重叠。
    事件日志用于把断言强绑定到「擦除后、下一次候选点击前发生了一次画笔补描」。
    干扰点落在撤销预扫描安全区（x < 画布左缘 + 一个网格步长）之外，仍走折叠
    钮盲扫路径。
    """

    SUPPRESS = (70, 512)
    BAND_RECT = (46, 70, 200, 40)

    def __init__(self) -> None:
        super().__init__()
        self.suppress_hits = 0
        self.events: list[tuple] = []

    def band_ink_px(self) -> int:
        x0, y0, w, h = self.BAND_RECT
        region = self.canvas_pixels[y0 : y0 + h, x0 : x0 + w]
        return int((region < 160).sum())

    def _erase_half_band(self) -> None:
        x0, y0, w, h = self.BAND_RECT
        region = self.canvas_pixels[y0 : y0 + h, x0 : x0 + w]
        dark_cols = np.flatnonzero((region < 160).any(axis=0))
        if dark_cols.size == 0:
            return
        cut = int(dark_cols[dark_cols.size // 2])
        region[:, cut + 1 :] = 255

    def handle_click(self, x: float, y: float) -> None:
        x0, y0, w, h = self.BAND_RECT
        if not (x0 <= x <= x0 + w and y0 <= y <= y0 + h):
            # 只记录工具条区域的按下；刻度带内的按下属于描线。
            self.events.append(("click", float(x), float(y)))
        if abs(x - self.SUPPRESS[0]) <= 2 and abs(y - self.SUPPRESS[1]) <= 2:
            self.suppress_hits += 1
            self.tool = "eraser"
            self._erase_half_band()
            self.events.append(("erase", self.band_ink_px()))
            return
        super().handle_click(x, y)

    def apply_stroke(self, x0: float, y0: float, x1: float, y1: float) -> None:
        super().apply_stroke(x0, y0, x1, y1)
        self.events.append(("stroke", self.tool, self.band_ink_px()))


class EmptyBoard(ToolbarBoard):
    """完全没有工具条：无滑杆、无折叠钮。"""

    def image(self) -> np.ndarray:
        return np.full((700, 600), 255, dtype=np.uint8)

    def handle_click(self, x: float, y: float) -> None:
        return


class NoSliderBoard(ToolbarBoard):
    """有折叠钮与工具格，但任何状态下都不画滑杆。"""

    def image(self) -> np.ndarray:
        img = super().image()
        x, y, w, h = self.SLIDER
        img[y : y + h, x : x + w] = 255
        return img


def test_locate_slider_excludes_label_and_spans_whole_track() -> None:
    """真机回归：轨道左邻「大小」标签、右段浅灰、圆点白缺口，三者都不得破坏定位。

    旧实现按「首暗列..末暗列」取 x 跨度，把 30px 外的文字标签并入轨道（x 左移
    ~75px，标定首档点到标签上成空操作、宽度表非单调、绘制期换宽不生效）；
    strict 档又只看得到黑色已填充段（右缘=圆点位置，半轨），映射整体压缩。
    期望：有撤销锚点时走宽松档，给出**不含标签的完整轨道**（黑色段+灰色段，
    跨越圆点缺口）。
    """
    image = np.full((1357, 720, 3), 255, np.uint8)
    # 轨道：黑色已填充段 99..594，圆点白缺口 595..640，浅灰未填充段 641..699
    image[1199:1207, 99:595] = 0
    image[1199:1207, 641:700] = 226
    # 「大小」文字标签：高 26px 的竖笔画（比轨道高，纵向延伸远超带高）
    for x0 in (36, 64):
        image[1190:1216, x0 : x0 + 3] = 0

    class _ImageBoard:
        def __init__(self) -> None:
            self.image_data = image
            self.clicks: list[tuple[float, float]] = []
            self.open = True

        def image(self) -> np.ndarray:
            return self.image_data

        def click(self, x: float, y: float) -> None:
            self.clicks.append((x, y))

    prober, _mouse = _prober(_ImageBoard())  # type: ignore[arg-type]
    prober._canvas = (96, 202, 531, 770)
    prober._slider_anchor = 924.0

    rect = prober._locate_slider(image)
    assert rect is not None
    assert rect["x"] >= 90, f"轨道左缘被文字标签污染: {rect}"
    assert rect["x"] <= 105
    # 完整轨道：右缘须越过圆点到灰色段末端（strict 档只会给到圆点 ~595）
    assert rect["x"] + rect["w"] >= 690, f"未覆盖浅灰未填充段: {rect}"


class FarFoldBoard(ToolbarBoard):
    """折叠钮落在候选序第 10 行（预算外），预算内不应被扫到。"""

    FOLD = (560, 680, 40, 40)


class InitialBoard(ToolbarBoard):
    """真实小程序初始态：底部只有 画笔/文字/贴纸/提交创作 标签，无绘制面板。

    点中最左「画笔」标签才进入绘画面板（= ToolbarBoard 展开态）；「文字」
    「贴纸」标签一旦点中记为 wrongly_clicked（探测绝不该碰到它们）。
    """

    BRUSH_TAB = (10, 650, 40, 40)
    TEXT_TAB = (140, 650, 40, 40)
    STICKER_TAB = (260, 650, 40, 40)

    def __init__(self) -> None:
        super().__init__()
        self.in_painting = False
        self.wrongly_clicked: list[tuple[float, float]] = []

    def image(self) -> np.ndarray:
        if self.in_painting:
            return super().image()
        img = np.full((700, 600), 248, dtype=np.uint8)
        img[CANVAS[1] : CANVAS[1] + CANVAS[3], CANVAS[0] : CANVAS[0] + CANVAS[2]] = 255
        for rect in (self.BRUSH_TAB, self.TEXT_TAB, self.STICKER_TAB):
            x, y, w, h = rect
            img[y + 8 : y + h, x : x + w] = 60  # 深色标签文字块
        return img

    def handle_click(self, x: float, y: float) -> None:
        if not self.in_painting:
            if self._hit(self.BRUSH_TAB, x, y):
                self.in_painting = True
                self.panel_open = True
                return
            if self._hit(self.TEXT_TAB, x, y) or self._hit(self.STICKER_TAB, x, y):
                self.wrongly_clicked.append((x, y))
                return
            return
        super().handle_click(x, y)

    def apply_stroke(self, *args) -> None:  # 初始态标签不参与描线
        if self.in_painting:
            super().apply_stroke(*args)


class ConfirmBoard(ToolbarBoard):
    """工具行最右放「✓ 确认」不可逆控件（候选点 x=518 在 reserve 536 之内）。

    真机 ✓ 粗黑、内容量高于细灰开合钮，行内降序预排会让它先被点中；右缘
    reserve 单独挡不住（518 ≤ 536），必须靠「内容行最右格」结构先验剔除。
    """

    CONFIRM = (508, 500, 28, 24)  # 罩住候选点 (518, 512)

    def __init__(self) -> None:
        super().__init__()
        self.confirmed = 0

    def image(self) -> np.ndarray:
        img = super().image()
        x, y, w, h = self.CONFIRM
        img[y : y + h, x : x + w] = 0  # 粗黑 ✓
        return img

    def handle_click(self, x: float, y: float) -> None:
        if self._hit(self.CONFIRM, x, y):
            self.confirmed += 1
            return
        super().handle_click(x, y)


class CapsuleBoard(ToolbarBoard):
    """撤销胶囊浮在画布**内**左下角（真机展开态形态）：x 跨画布左缘、y 在画布内。

    主候选网格只覆盖画布下方，预扫描安全区（画布下方左缘列）也够不着；
    只有胶囊带候选能命中——且命中点 (42,344) 落在画布内，必须走胶囊带
    豁免通道。胶囊左半为撤销、右半为重做（本板不模拟重做效果）。
    """

    CAPSULE = (36, 330, 114, 50)  # 画布 (40,60,300,360) 内左下角，略跨左缘
    UNDO_HALF = (36, 330, 57, 50)

    def image(self) -> np.ndarray:
        img = super().image()
        x, y, w, h = self.CAPSULE
        img[y + 8 : y + h - 8, x + 10 : x + 50] = 120  # 撤销图标块
        img[y + 8 : y + h - 8, x + 64 : x + 104] = 120  # 重做图标块
        return img

    def handle_click(self, x: float, y: float) -> None:
        if self._hit(self.UNDO_HALF, x, y):
            self.undo()
            return
        super().handle_click(x, y)


class InitialNoFoldBoard(InitialBoard):
    """初始态进入的面板没有开合钮，轨道是 strict 检不到的浅灰宽段（235）。

    brush_entry 必须靠结构判据确认入场（不得要求折叠钮重探通过），并凭
    入场时定带的锚点用宽松档定位浅灰滑杆——开合钮缺失只报 fold failed，
    不再整链 E_PROBE_NO_TOOLBAR 报废。
    """

    FOLD = (0, 0, 0, 0)  # 无开合钮

    def image(self) -> np.ndarray:
        img = super().image()
        if self.in_painting:
            x, y, w, h = self.SLIDER
            img[y : y + h, x : x + w] = 235  # 浅灰轨道：strict 失明、宽松档/结构 oracle 可见
        return img


class FarCollapsedBoard(ToolbarBoard):
    """收起态工具行远在 4 行折叠预算之外（真机窄高窗：画布增高把工具行推远）。

    展开态与基类一致（工具行 y495-535 + 滑杆）；收起态工具行整体下移 160px
    到 y655-695（距画布底 235px），展开钮随行。基类静止的 FOLD 残影在收起帧
    抹掉且不可点——开合钮两态不同位（真机 ∨ 在 y1030、∧ 随行到 y1280）。
    撤销格放最左列：预扫描先命中它建立锚点，主折叠扫描按锚点定带覆盖深行
    （与真机成功路径同形），收起态重扫则只能靠内容行扫描面够到展开钮。
    """

    SHIFT = -160  # 负值 = 收起态工具行下移（真机方向）
    UNDO = (10, 495, 40, 40)  # 最左列，预扫描可达
    UNFOLD_COLLAPSED = (300, 645, 24, 40)

    def image(self) -> np.ndarray:
        img = super().image()
        if not self.panel_open:
            x, y, w, h = self.FOLD
            img[y : y + h, x : x + w] = 255  # 抹掉基类静止残影
            ux, uy, uw, uh = self.UNFOLD_COLLAPSED
            img[uy : uy + uh, ux : ux + uw] = 235
        return img

    def handle_click(self, x: float, y: float) -> None:
        if self._hit(self.FOLD, x, y):
            # 开合钮两态不同位：收起态点展开态位置（残影）不生效。
            if self.panel_open:
                self.panel_open = False
            return
        if not self.panel_open and self._hit(self.UNFOLD_COLLAPSED, x, y):
            self.panel_open = True
            return
        super().handle_click(x, y)


class RelayoutBoard(ToolbarBoard):
    """收起态点工具格会展开面板（真机行为），撤销只以胶囊形式存在于展开态。

    起始为收起态：无滑杆、无撤销胶囊，工具格上移。点中画笔/橡皮擦格 →
    面板展开 → 滑杆出现 + 撤销胶囊浮进画布内左下角。旧帧候选网格里胶囊
    位置是画布内部/空白，只有重构感知重扫能学到撤销。
    """

    CAPSULE = (36, 330, 114, 50)
    UNDO_HALF = (36, 330, 57, 50)

    def __init__(self) -> None:
        super().__init__()
        self.panel_open = False

    def image(self) -> np.ndarray:
        img = super().image()
        # 工具格态的撤销方块不存在（撤销只在展开态胶囊里）。
        x, y, w, h = self._tool_rect(self.UNDO)
        img[y : y + h, x : x + w] = 255
        if self.panel_open:
            cx, cy, cw, ch = self.CAPSULE
            img[cy + 8 : cy + ch - 8, cx + 10 : cx + 50] = 120  # 撤销图标
            img[cy + 8 : cy + ch - 8, cx + 64 : cx + 104] = 120  # 重做图标
        return img

    def handle_click(self, x: float, y: float) -> None:
        if self.panel_open and self._hit(self.UNDO_HALF, x, y):
            self.undo()
            return
        if self._hit(self._tool_rect(self.UNDO), x, y):
            return  # 工具格态没有撤销
        if not self.panel_open and (
            self._hit(self._tool_rect(self.BRUSH), x, y)
            or self._hit(self._tool_rect(self.ERASER), x, y)
        ):
            self.panel_open = True  # 收起态点工具格 → 展开面板（真机行为）
        super().handle_click(x, y)


class ToolbarMouse:
    def __init__(self, board: ToolbarBoard) -> None:
        self.board = board
        self.pos = (0.0, 0.0)
        self.down_flag = False
        self.press_pos = (0.0, 0.0)

    def move_to(self, x, y):
        self.pos = (float(x), float(y))

    def move_sample(self, x, y, no_coalesce=True):
        if self.down_flag:
            self.board.apply_stroke(self.pos[0], self.pos[1], float(x), float(y))
        self.pos = (float(x), float(y))

    def down(self):
        self.board.handle_click(*self.pos)
        self.board.pending = []
        self.board.pending_touched = False
        self.press_pos = self.pos
        self.down_flag = True

    def up(self):
        if self.board.pending:
            # 与真实画板一致：每笔都进撤销历史，哪怕没改变像素（重叠重描、
            # 橡皮空划）。探测的撤销预算按发出笔数计，桩若只为变化记账，
            # 预算与历史深度就不匹配，掩盖真机「撤不干净」的问题。
            self.board.strokes.append(self.board.pending)
        else:
            # 无位移的按下-抬起才算一次「点击」；带位移的按下属于描线。
            self.board.clicks.append(self.press_pos)
        self.board.pending = []
        self.down_flag = False


class ToolbarProbe:
    def __init__(self, board: ToolbarBoard) -> None:
        self.board = board

    def capture_image(self):
        return self.board.image()

    def foreground_matches(self) -> bool:
        return True

    def activate(self) -> bool:
        return True


def _prober(board: ToolbarBoard) -> tuple[ToolbarProber, ToolbarMouse]:
    probe = ToolbarProbe(board)
    mouse = ToolbarMouse(board)
    prober = ToolbarProber(
        probe=probe, mouse=mouse, to_screen=lambda x, y: (x, y),
        sleep=lambda _s: None,
    )
    return prober, mouse


def _run(
    board: ToolbarBoard,
    *,
    open_bitmap: ToolBitmap | None = None,
    should_stop=lambda: False,
    steps: list[dict] | None = None,
    rescan_collapsed: bool = False,
    undo_prescan: bool = True,
) -> ToolBitmap:
    prober, _ = _prober(board)
    return prober.run(
        canvas=CANVAS,
        band=BAND,
        open_bitmap=open_bitmap,
        on_step=(steps.append if steps is not None else (lambda _e: None)),
        should_stop=should_stop,
        rescan_collapsed=rescan_collapsed,
        undo_prescan=undo_prescan,
    )


def _manual(**overrides) -> ToolBitmap:
    fields = dict(
        brush=None, eraser=None, slider=None, undo=None, fold=None,
        brush_selected_lum=None, brush_idle_lum=None,
    )
    fields.update(overrides)
    return ToolBitmap(**fields)


def _inside(rect, point) -> bool:
    x0, y0, w, h = rect
    x, y = point
    return x0 <= x <= x0 + w and y0 <= y <= y0 + h


# ---------- 用例 ----------


def test_probe_finds_fold_and_slider():
    board = ToolbarBoard()
    steps: list[dict] = []
    result = _run(board, steps=steps)
    assert result.fold is not None
    assert _inside(ToolbarBoard.FOLD, result.fold)
    assert result.slider == {"x": 100, "y": 530, "w": 200, "h": 12}
    # 面板最终应回到展开态（探测会点两次折叠钮以验证可逆）。
    assert board.panel_open
    assert _step_of(steps, "fold")["state"] == "done"
    assert _step_of(steps, "slider")["state"] == "done"


def _step_of(steps: list[dict], name: str) -> dict:
    """取指定步骤的最后一条事件（同一步骤只会上报一次终态）。"""
    matches = [event for event in steps if event["step"] == name]
    assert matches, f"missing step event: {name}"
    return matches[-1]


def test_probe_finds_undo_by_ink_drop():
    board = ToolbarBoard()
    result = _run(board)
    assert result.undo is not None
    assert _inside(ToolbarBoard.UNDO, result.undo)
    # 判定依赖真实触发了一次撤销（墨量下降）。
    assert board.undos >= 1


def test_undo_prescan_leads_and_anchors_fold_scan():
    """真机窄高窗口：撤销行距画布数百像素，盲扫预算够不着——预扫描必须先把
    撤销学到手（步骤先于 fold 上报 done），折叠钮随后按撤销行锚点定带命中。"""

    class TallBoard(ToolbarBoard):
        """画布下方 300+px 空白，撤销行/工具行都在深部（仿 720x1353 真机）。

        撤销胶囊独立成行且左缘探出画布左缘（预扫描安全区覆盖），折叠钮与
        工具格在撤销行下方一行——与真机「撤销恒在开合钮上方」的结构一致。
        """

        UNDO = (10, 845, 140, 24)
        FOLD = (300, 895, 24, 40)
        BRUSH = (10, 895, 40, 40)
        ERASER = (60, 895, 40, 40)
        SLIDER = (100, 940, 200, 12)
        SHIFT = 40

        def __init__(self) -> None:
            super().__init__()
            self.canvas_pixels = np.full((1000, 600), 255, dtype=np.uint8)

    board = TallBoard()
    steps: list[dict] = []
    result = _run(board, steps=steps)
    assert result.undo is not None and _inside(TallBoard.UNDO, result.undo)
    assert result.fold is not None and _inside(TallBoard.FOLD, result.fold)
    undo_done = [
        i for i, e in enumerate(steps)
        if e["step"] == "undo" and e["state"] == "done"
    ]
    fold_done = [
        i for i, e in enumerate(steps)
        if e["step"] == "fold" and e["state"] == "done"
    ]
    assert undo_done and fold_done and undo_done[0] < fold_done[0]
    assert "先行识别" in next(
        e["evidence"] for e in steps if e["step"] == "undo" and e["state"] == "done"
    )


def test_relaxed_slider_requires_undo_anchor_and_rejects_full_width():
    """宽松档：无撤销锚点不启用；通宽长条（画布阴影带）否决；撤销行以下浅灰轨道命中。"""
    board = ToolbarBoard()
    prober, _ = _prober(board)
    prober._canvas = CANVAS
    prober._band = BAND
    image = np.full((700, 600), 255, dtype=np.uint8)
    # 画布下缘阴影：通宽浅灰带（600px > 86% 上限），任何档都不该当轨道。
    image[428:436, 0:600] = 232
    assert prober._locate_slider(image) is None  # 无锚点：宽松档不启用
    prober._slider_anchor = 500.0
    assert prober._locate_slider(image) is None  # 通宽带否决
    image[520:530, 120:500] = 234  # 撤销行以下、380px 宽（63%）的浅灰轨道
    rect = prober._locate_slider(image)
    assert rect is not None
    assert rect["x"] == 120 and rect["w"] == 380
    # 宽松档绝不污染轨道可见性 oracle（收起帧工具格不得被误判为轨道）。
    assert prober._find_slider(image) is None


def test_probe_finds_eraser_by_line_erase():
    board = ToolbarBoard()
    result = _run(board)
    assert result.eraser is not None
    assert _inside(ToolbarBoard.ERASER, result.eraser)
    # 压线重描后线白化，说明该候选切到了橡皮。
    assert result.brush is not None
    assert _inside(ToolbarBoard.BRUSH, result.brush)


def test_probe_brush_fingerprints():
    board = ToolbarBoard()
    result = _run(board)
    assert result.brush is not None
    assert _inside(ToolbarBoard.BRUSH, result.brush)
    assert result.brush_selected_lum is not None
    assert result.brush_idle_lum is not None
    assert abs(result.brush_selected_lum - ToolbarBoard.SELECTED_LUM) < 1
    assert abs(result.brush_idle_lum - ToolbarBoard.IDLE_LUM) < 1
    assert result.brush_idle_lum - result.brush_selected_lum >= ToolbarProber.LUM_DELTA_MIN


def test_probe_respects_manual_items():
    board = ToolbarBoard()
    manual = ToolBitmap(
        brush=(30, 515), eraser=(80, 515),
        slider={"x": 100, "y": 530, "w": 200, "h": 12},
        undo=(130, 515), fold=(312, 512),
        brush_selected_lum=180.0, brush_idle_lum=250.0,
    )
    result = _run(board, open_bitmap=manual)
    assert result == manual
    assert board.clicks == []  # 全部给定则不点任何候选

    # 仅人工给定滑杆：折叠钮仍应探测，且人工控件邻域候选不应被点。
    board2 = ToolbarBoard()
    partial = ToolBitmap(
        brush=(30, 515), eraser=(80, 515),
        slider={"x": 100, "y": 530, "w": 200, "h": 12},
        undo=(130, 515), fold=None,
        brush_selected_lum=180.0, brush_idle_lum=250.0,
    )
    result2 = _run(board2, open_bitmap=partial)
    assert result2.slider == partial.slider
    assert result2.fold is not None
    assert _inside(ToolbarBoard.FOLD, result2.fold)
    assert board2.clicks
    # 人工画笔/橡皮/撤销/滑杆全部进入折叠阶段跳过集合。
    assert (14.0, 512.0) not in board2.clicks
    assert (42.0, 512.0) not in board2.clicks
    assert (70.0, 512.0) not in board2.clicks
    assert (126.0, 512.0) not in board2.clicks


def test_probe_clicks_only_outside_canvas():
    board = ToolbarBoard()
    _run(board)
    assert board.clicks
    assert all(not _inside(CANVAS, point) for point in board.clicks)
    assert all(point[1] > CANVAS_BOTTOM for point in board.clicks)


def test_fold_scan_reorders_within_budget_only():
    """按行墨量提速扫描**只许改顺序**；覆盖面 = 预算窗口 − 右缘守卫。

    候选预算（距画布最近 N 行）是安全边界：更远的窗口底部一行可能停着
    「文字/贴纸」等不可逆标签，只能由带差分门控的 brush_entry 流程处理。
    右缘守卫（reserve 列 + 内容行最右格）在安全边界内再缩面，同样不得外扩。
    """
    image = np.full((700, 600), 255, dtype=np.uint8)
    prober, _ = _prober(ToolbarBoard())
    prober._canvas = CANVAS
    prober._band = BAND
    prober._candidates = prober._make_candidates(image)
    prober._fold_limit = 66  # 预算止于空白行，按钮行落在预算之外
    budget = prober._candidates[: prober._fold_limit]

    ordered = prober._fold_scan_candidates()

    # 全白帧无内容格：守卫只剔右缘 reserve 列（x > 600-64 的 546/574 两列）。
    reserve = max(
        ToolbarProber.SCAN_RIGHT_RESERVE_PX,
        int(prober._image_width * ToolbarProber.SCAN_RIGHT_RESERVE_RATIO),
    )
    expected = [p for p in budget if p[0] <= prober._image_width - reserve]
    assert len(ordered) == len(expected)
    assert set(ordered) == set(expected)
    assert all(point[1] <= max(p[1] for p in budget) for point in ordered)


def test_fold_scan_prefers_content_rows_over_blank_rows():
    """工具条各行（有图标/滑杆像素）必须排在全空白行之前，避免鼠标先在空白区爬行。"""
    board = ToolbarBoard()
    prober, _ = _prober(board)
    prober._canvas = CANVAS
    prober._band = BAND
    prober._candidates = prober._make_candidates(board.image())
    prober._fold_limit = len(prober._candidates)  # 全量，仅为比较行序

    ordered = prober._fold_scan_candidates()

    rows_in_order: list[int] = []
    for _x, y in ordered:
        if not rows_in_order or rows_in_order[-1] != y:
            rows_in_order.append(y)
    content_rows = [y for y, ink in prober._row_ink.items() if ink > 0]
    blank_rows = [y for y, ink in prober._row_ink.items() if ink == 0]
    assert content_rows and blank_rows, "夹具应同时含工具条行与空白行"
    assert max(rows_in_order.index(y) for y in content_rows) < min(
        rows_in_order.index(y) for y in blank_rows
    )


def test_probe_skips_known_control_neighborhood():
    board = ToolbarBoard()
    prober, _ = _prober(board)
    prober._canvas = CANVAS
    prober._candidates = prober._make_candidates(np.full((700, 600), 255, dtype=np.uint8))
    skip = prober._skip_points(
        brush=(14, 512),
        eraser=(70, 512),
        undo=(126, 512),
        fold=(322, 512),
        slider={"x": 100, "y": 530, "w": 200, "h": 12},
    )
    # 宽按钮内相邻候选（相距一个网格步长）必须一起跳过。
    assert (42, 512) in skip
    assert (14, 484) in skip
    assert (98, 512) in skip
    assert (350, 512) in skip
    # 滑杆矩形内的候选也跳过。
    assert (126, 540) in skip
    assert (182, 512) not in skip


def test_probe_continues_after_irreversible_fold_candidate():
    board = FoldInterferenceBoard()
    result = _run(board)
    assert result.fold is not None
    assert _inside(ToolbarBoard.FOLD, result.fold)
    # 干扰点只做一次翻转 + 一次恢复尝试即放弃（之后不再触发干扰）。
    assert board.interference_hits == 2


def test_probe_learns_independent_unfold_button():
    """「折叠」「展开」为两个独立按钮：折叠钮学成 fold，展开钮被学到并用于恢复。"""
    board = DualButtonBoard()
    result = _run(board)
    assert result.fold is not None and _inside(ToolbarBoard.FOLD, result.fold)
    # 展开钮被真实点击过（往返复验 + 后续开合），且探测以展开态收尾。
    assert board.unfold_hits >= 2
    assert board.panel_open


def test_fold_only_button_rejected_by_round_trip():
    """只能关不能开的候选过不了成对复验：不得当成折叠钮入库。"""
    board = FoldOnlyBoard()
    with pytest.raises(ProbeError) as exc:
        _run(board)
    assert exc.value.code == "E_PROBE_NO_TOOLBAR"


def test_brush_entry_from_initial_state():
    """初始态（只有 画笔/文字/贴纸）：折叠钮探测失败后自动点最左画笔进入面板。"""
    board = InitialBoard()
    steps: list[dict] = []
    result = _run(board, steps=steps)
    assert board.in_painting
    # 不可逆安全约束：右侧「文字/贴纸」标签绝不被触碰。
    assert board.wrongly_clicked == []
    # 任何落在底部标签行的点击都必须是最左两列（画笔标签带），绝不横移。
    assert all(
        x <= 2 * ToolbarProber.CANDIDATE_STEP
        for x, y in board.clicks
        if y >= 620
    )
    entry = _step_of(steps, "brush_entry")
    assert entry["state"] == "done"
    assert result.fold is not None and _inside(ToolbarBoard.FOLD, result.fold)
    assert result.slider is not None and result.undo is not None
    assert result.brush is not None and result.eraser is not None
    assert board.panel_open
    assert board.band_ink() == 0.0


def test_brush_entry_skipped_when_panel_present():
    """正常绘画面板存在时绝不触发入口试探（不点底部最左列的标签行）。"""
    board = ToolbarBoard()
    steps: list[dict] = []
    _run(board, steps=steps)
    assert all(item["step"] != "brush_entry" for item in steps)


def test_fold_scan_never_clicks_right_confirm():
    """「✓ 确认」在 reserve 之内、内容量高于开合钮：必须被最右格先验剔除。

    剥离该先验时本用例必挂（✓ 会先于折叠钮被点中），它是 reserve 之外的
    第二层右缘防线。
    """
    board = ConfirmBoard()
    steps: list[dict] = []
    result = _run(board, steps=steps)
    assert board.confirmed == 0
    assert (518.0, 512.0) not in board.clicks
    assert result.fold is not None and _inside(ToolbarBoard.FOLD, result.fold)


def test_undo_capsule_inside_canvas_found_via_strip():
    """撤销胶囊浮在画布内左下角：胶囊带候选 + 画布内点击豁免使命中成立。"""
    board = CapsuleBoard()
    steps: list[dict] = []
    result = _run(board, steps=steps)
    assert result.undo is not None
    assert _inside(CapsuleBoard.UNDO_HALF, result.undo)
    # 命中点在画布内——证明走了胶囊带豁免而非被拒/绕过。底缘优先行序下胶囊带
    # 两行按 y 降序进入，(42,372) 先行命中（旧 y 升序下是 (42,344)）；两态均
    # 为胶囊带登记的画布内点，豁免语义不变。
    hit = (float(result.undo[0]), float(result.undo[1]))
    assert _inside(CANVAS, hit)
    assert hit in board.clicks
    # 除胶囊带登记点外，其余点击仍在画布外。
    assert all(
        not _inside(CANVAS, point) or point in {(42.0, 344.0), (42.0, 372.0)}
        for point in board.clicks
    )
    assert board.band_ink() == 0.0


def test_wide_track_oracle_tolerates_light_gray_track():
    """strict 档对浅灰轨道失明时，结构 oracle 凭宽段行带判可见；窄格/通宽带不误判。"""
    prober, _ = _prober(ToolbarBoard())
    prober._canvas = CANVAS
    prober._band = BAND
    # 浅灰宽轨道（63% 宽，235 > strict 阈值 160）：strict 保持纯，结构 oracle 判可见。
    image = np.full((700, 600), 255, dtype=np.uint8)
    image[530:536, 120:500] = 235
    assert prober._find_slider(image) is None
    assert prober._wide_track_visible(image)
    # 通宽阴影带（>86% 上限）→ 不可见。
    image_full = np.full((700, 600), 255, dtype=np.uint8)
    image_full[428:434, 0:600] = 232
    assert not prober._wide_track_visible(image_full)
    # 窄工具格行（23% < 30% 下限，收起态工具行形态）→ 不可见。
    image_cells = np.full((700, 600), 255, dtype=np.uint8)
    image_cells[500:524, 10:150] = 235
    assert not prober._wide_track_visible(image_cells)


def test_brush_entry_structural_confirm_without_fold():
    """无开合钮 + 浅灰轨道：结构判据确认入场，宽松档定位滑杆，全链不报废。"""
    board = InitialNoFoldBoard()
    steps: list[dict] = []
    # 关掉撤销预扫描，强制走 _try_brush_entry 入口（预扫描误点标签会提前进
    # 面板，那条路径另有覆盖）。
    result = _run(board, steps=steps, undo_prescan=False)
    assert board.in_painting
    assert board.wrongly_clicked == []
    entry = _step_of(steps, "brush_entry")
    assert entry["state"] == "done"
    assert "结构判据" in entry["evidence"]
    # 开合钮不存在：fold 失败但不再是整链 E_PROBE_NO_TOOLBAR。
    assert result.fold is None
    assert _step_of(steps, "fold")["state"] == "failed"
    # 结构锚点定带 + 宽松档检出浅灰滑杆；撤销/工具格照常探测。
    assert result.slider is not None
    assert result.undo is not None
    assert result.brush is not None and result.eraser is not None
    assert board.band_ink() == 0.0


def test_undo_scan_prefers_left_column_over_tool_cells():
    """撤销扫描序：左缘列内容点（胶囊所在）先于内容量更高的工具行。

    工具行内容量天然高于胶囊行，若按内容量降序会先点中画笔/橡皮擦格——
    真机上收起态点工具格会展开面板、重构 UI，胶囊就此丢失。
    """
    board = ToolbarBoard()
    prober, _ = _prober(board)
    prober._canvas = CANVAS
    prober._band = BAND
    prober._candidates = prober._make_candidates(board.image())
    prober._undo_tried = set()
    ordered = prober._undo_scan_order()
    left_limit = CANVAS[0] + ToolbarProber.CANDIDATE_STEP
    left_pos = [i for i, p in enumerate(ordered) if p[0] < left_limit]
    tool_pos = [
        i for i, p in enumerate(ordered) if p[0] >= left_limit and p[1] in (484, 512)
    ]
    assert left_pos and tool_pos
    assert max(left_pos) < min(tool_pos)


def test_undo_scan_recovers_from_panel_relayout():
    """扫描期点击引发面板展开（UI 重构）：重算网格后仍能在新位置学到撤销。

    旧实现下胶囊随重构上移进画布内（旧帧网格盲区），撤销永远选不中。
    """
    board = RelayoutBoard()
    steps: list[dict] = []
    # 人工 fold/slider 隔离折叠/滑杆路径；关预扫描，强制走主撤销扫描。
    result = _run(
        board,
        open_bitmap=_manual(
            fold=(312, 512), slider={"x": 100, "y": 530, "w": 200, "h": 12}
        ),
        steps=steps,
        undo_prescan=False,
    )
    assert result.undo is not None
    assert _inside(RelayoutBoard.UNDO_HALF, result.undo)
    # 先点中收起态工具格（触发展开），重构重扫后才点中画布内胶囊
    # （胶囊带按 y 降序构建，(42,372) 先于 (42,344) 命中撤销半区）。
    assert (42.0, 456.0) in board.clicks
    assert (42.0, 372.0) in board.clicks
    assert board.clicks.index((42.0, 456.0)) < board.clicks.index((42.0, 372.0))
    assert board.panel_open
    assert board.band_ink() == 0.0


def test_capsule_strip_reaches_four_rows():
    """胶囊带深度 4 行：胶囊浮得更高（距画布底缘 3~4 行）也能登记并获得豁免。"""
    board = ToolbarBoard()
    prober, _ = _prober(board)
    prober._canvas = CANVAS
    prober._band = BAND
    image = np.full((700, 600), 255, dtype=np.uint8)
    image[302:330, 40:80] = 150  # 第 4 行（y=316）处的胶囊图标块
    prober._candidates = prober._make_candidates(image)
    assert (42, 316) in prober._strip_candidates
    assert (42, 316) in prober._strip_points
    assert (14, 316) not in prober._strip_points  # 邻域够不着图标块
    # 登记点在画布内但可点（豁免）；未登记的画布内点仍被硬约束拒绝。
    assert _inside(CANVAS, (42.0, 316.0))
    prober._click(42.0, 316.0)
    with pytest.raises(ProbeError) as exc:
        prober._click(100.0, 100.0)
    assert exc.value.code == "E_PROBE_CLICK_IN_CANVAS"


def test_rescan_collapsed_learns_independent_unfold_button():
    """收起态位图的 fold 必须是收起态生效的「展开」钮，而不是展开态折叠钮。"""
    board = DualButtonBoard()
    steps: list[dict] = []
    result = _run(board, steps=steps, rescan_collapsed=True)
    collapsed = result.collapsed
    assert collapsed is not None
    assert _inside(ToolbarBoard.FOLD, result.fold)
    assert collapsed.fold is not None
    assert _inside(DualButtonBoard.UNFOLD_RECT, collapsed.fold)
    assert collapsed.undo is not None and _inside(
        _collapsed_rect(ToolbarBoard.UNDO), collapsed.undo
    )
    assert board.panel_open
    assert board.band_ink() == 0.0
    assert _step_of(steps, "collapsed")["state"] == "done"


def test_rescan_collapsed_finds_unfold_beyond_fold_budget():
    """收起态工具行平移到 4 行预算之外（真机 ~250px）：展开钮扫描走内容行。

    旧实现的盲扫预算只覆盖画布下 4 行，收起帧里工具行整体平移到预算之外，
    展开钮永远扫不到（真机失败现场：「收起帧未找到展开按钮」，89 个候选全
    是空白带）。
    """
    board = FarCollapsedBoard()
    steps: list[dict] = []
    result = _run(board, steps=steps, rescan_collapsed=True)
    collapsed = result.collapsed
    assert collapsed is not None
    assert collapsed.fold is not None
    assert _inside(FarCollapsedBoard.UNFOLD_COLLAPSED, collapsed.fold)
    assert _step_of(steps, "collapsed")["state"] == "done"
    assert board.panel_open
    assert board.band_ink() == 0.0


def test_probe_cleans_band_and_stroke_history():
    board = ToolbarBoard()
    result = _run(board)
    assert result.brush is not None
    assert result.undo is not None
    assert board.strokes == []
    assert board.band_ink() == 0.0
    assert board.tool == "brush"


def test_probe_cleanup_keeps_user_strokes():
    board = ToolbarBoard()
    # 用户先画两笔（画布别处），刻度带仍干净。撤销按钮由人工给定，
    # 以隔离「清理阶段预算」这条路径：折叠扫描不会盲点撤销消耗用户历史。
    board.canvas_pixels[200:203, 100:160] = 0
    board.strokes.append([(200, 203, 100, 160, np.full((3, 60), 255, dtype=np.uint8))])
    board.canvas_pixels[300:303, 150:210] = 0
    board.strokes.append([(300, 303, 150, 210, np.full((3, 60), 255, dtype=np.uint8))])
    user_strokes = list(board.strokes)
    assert board.band_ink() == 0.0

    result = _run(board, open_bitmap=_manual(undo=(130, 515)))
    assert result.brush is not None
    assert result.undo == (130.0, 515.0)
    # 探测只撤自己的笔迹：用户两笔必须完整留在撤销历史里。
    assert board.strokes == user_strokes
    assert board.band_ink() == 0.0
    assert board.canvas_pixels[200:203, 100:160].max() == 0
    assert board.canvas_pixels[300:303, 150:210].max() == 0


def test_probe_cleanup_restores_band_baseline():
    board = ToolbarBoard()
    # 探测前刻度带里已有一笔用户墨迹：同时写入像素与撤销历史，模拟真实的一笔。
    prev = board.canvas_pixels[88:93, 54:100].copy()
    board.canvas_pixels[88:93, 54:100] = 0
    baseline_stroke = (88, 93, 54, 100, prev)
    board.strokes.append([baseline_stroke])
    baseline_ink = board.band_ink()
    assert baseline_ink > 0

    result = _run(board, open_bitmap=_manual(undo=(130, 515)))
    assert result.brush is not None
    # 清理只撤探测自己新增的笔迹：用户基线笔仍在历史里，带内墨量回到基线。
    assert board.strokes == [[baseline_stroke]]
    assert abs(board.band_ink() - baseline_ink) < 1e-9


def test_probe_keeps_manual_lum_and_only_nulls_computed():
    # 人工 selected 与自动 idle 差不足：只把计算所得 idle 置 None，保留人工值。
    board = ToolbarBoard()
    result = _run(board, open_bitmap=_manual(brush_selected_lum=240.0))
    assert result.brush_selected_lum == 240.0
    assert result.brush_idle_lum is None
    assert result.brush is not None

    # 差足够时保留人工 selected 并使用计算 idle。
    board2 = ToolbarBoard()
    result2 = _run(board2, open_bitmap=_manual(brush_selected_lum=123.0))
    assert result2.brush_selected_lum == 123.0
    assert result2.brush_idle_lum is not None


def test_probe_ends_with_brush_when_lums_manual():
    board = ToolbarBoard()
    board.tool = "eraser"  # 从橡皮态开始，工具探测会以橡皮扫描收尾。
    result = _run(
        board,
        open_bitmap=_manual(
            undo=(130, 515),
            brush_selected_lum=180.0,
            brush_idle_lum=250.0,
        ),
    )
    assert result.brush is not None
    assert result.eraser is not None
    assert board.tool == "brush"


def test_probe_restores_drawing_tool_before_supplement():
    board = SuppressingBoard()
    result = _run(board)
    # 干扰点确实擦掉了测试线的一部分（落在基线 40%~90% 的部分丢失区间）。
    assert board.suppress_hits >= 1

    # 折叠阶段可能先盲点到干扰点（此时刻度带还空），取真正擦掉墨的那一次。
    erase_idx = next(
        i for i, e in enumerate(board.events) if e[0] == "erase" and e[1] > 0
    )
    erased_ink = board.events[erase_idx][1]
    full_ink = max(e[2] for e in board.events[:erase_idx] if e[0] == "stroke")
    assert erased_ink < full_ink * 0.9

    # 擦除后探测器必须恢复画笔态并把墨量补回基线附近，且发生在撤销判定
    # （点击撤销候选 (126,512)）之前。新流程里干扰点 (70,512) 落在折叠钮
    # 盲扫路径上（撤销预扫描只扫画布左缘列），橡皮态由其造成；恢复走
    # _ensure_drawing_tool 扫到画笔格 (14,512) 后补描完成。
    anchor_idx = next(
        i
        for i in range(erase_idx + 1, len(board.events))
        if board.events[i] == ("click", 126.0, 512.0)
    )
    window = board.events[erase_idx + 1 : anchor_idx]
    assert any(
        e[0] == "stroke" and e[1] == "brush" and e[2] >= full_ink * 0.9
        for e in window
    ), "补描分支缺失：擦除后到撤销判定前没有画笔落笔把墨量补回基线"

    # 补描后继续扫描，撤销按钮仍能被正确判定，且最终笔迹清理干净。
    assert result.undo is not None
    assert _inside(ToolbarBoard.UNDO, result.undo)
    assert board.undos >= 1
    assert board.band_ink() == 0.0
    assert board.strokes == []


def test_probe_rejects_manual_point_inside_canvas():
    board = ToolbarBoard()
    result = _run(
        board,
        open_bitmap=_manual(fold=(100, 100), brush=(50, 100)),
    )
    # 画布内的人工点被丢弃，改由探测得到真实控件。
    assert result.fold is not None
    assert _inside(ToolbarBoard.FOLD, result.fold)
    assert result.brush is not None
    assert _inside(ToolbarBoard.BRUSH, result.brush)
    assert (100.0, 100.0) not in board.clicks
    assert (50.0, 100.0) not in board.clicks
    assert all(not _inside(CANVAS, point) for point in board.clicks)


def test_probe_click_guard_rejects_canvas_points():
    board = ToolbarBoard()
    prober, _ = _prober(board)
    prober._canvas = CANVAS
    with pytest.raises(ProbeError) as exc:
        prober._click(100.0, 100.0)
    assert exc.value.code == "E_PROBE_CLICK_IN_CANVAS"
    assert board.clicks == []


def test_probe_rejects_malformed_manual_slider():
    board = ToolbarBoard()
    with pytest.raises(ProbeError) as exc:
        _run(board, open_bitmap=_manual(slider={"x": 100}))
    assert exc.value.code == "E_PROBE_BAD_MANUAL"
    assert board.clicks == []


def test_probe_raises_when_probe_ink_uncleanable():
    board = NoUndoBoard()
    with pytest.raises(ProbeError) as exc:
        _run(
            board,
            open_bitmap=_manual(
                fold=(312, 512),
                slider={"x": 100, "y": 530, "w": 200, "h": 12},
                brush=(30, 515),
                eraser=(80, 515),
                brush_selected_lum=180.0,
                brush_idle_lum=250.0,
            ),
        )
    # undo 未知且刻度带留有测试笔迹：必须上报，而不是留下无法清理的墨。
    assert exc.value.code == "E_PROBE_UNCLEANED"


def test_probe_raises_no_toolbar():
    board = EmptyBoard()
    with pytest.raises(ProbeError) as exc:
        _run(board)
    assert exc.value.code == "E_PROBE_NO_TOOLBAR"


def test_probe_raises_no_slider():
    board = NoSliderBoard()
    with pytest.raises(ProbeError) as exc:
        _run(board, open_bitmap=_manual(fold=(312, 512)))
    assert exc.value.code == "E_PROBE_NO_SLIDER"


def _fold_cols(width: int) -> int:
    """与 ToolbarProber.run 相同的每行候选列数上界。"""
    return max(1, math.ceil(width / ToolbarProber.CANDIDATE_STEP))


def test_probe_fold_scan_respects_click_budget():
    """折叠钮在预算外：预扫描（安全区）+ 画布下方最近 MAX_FOLD_ROWS 行，然后放弃。"""
    board = FarFoldBoard()
    board.panel_open = False
    height, width = board.canvas_pixels.shape
    assert FarFoldBoard.FOLD[1] >= CANVAS_BOTTOM + 8 + ToolbarProber.MAX_FOLD_ROWS * ToolbarProber.CANDIDATE_STEP

    with pytest.raises(ProbeError) as exc:
        _run(board)

    assert exc.value.code == "E_PROBE_NO_TOOLBAR"
    # 撤销预扫描（安全区 x < 画布左缘 40 的内容点：(14,456)/(14,484)）先点
    # 2 个；折叠钮扫描跳过这些已证伪点，只补足预算内其余候选；画笔入口试探
    # 底部 3 行 × 最左 2 列，全部无像素反馈被差分门控否决。
    assert board.clicks.count((14.0, 456.0)) == 1
    assert board.clicks.count((14.0, 484.0)) == 1
    # 总点数推导：预算窗口 88 点（4 整行 ×21 列 + 下行前 4 点，点制截断）；
    # 右缘守卫剔除 x∈{546,574} 两列共 4×2+0=8 点（下行前 4 点都在 reserve 内），
    # 窗口余 80；折叠扫描再跳过预扫描证伪的 2 点 → 78；+ 预扫描 2 + 入口 3×2。
    step = ToolbarProber.CANDIDATE_STEP
    reserve = max(
        ToolbarProber.SCAN_RIGHT_RESERVE_PX,
        int(width * ToolbarProber.SCAN_RIGHT_RESERVE_RATIO),
    )
    cols_all = len(range(step // 2, width, step))
    cols_kept = len([x for x in range(step // 2, width, step) if x <= width - reserve])
    window = ToolbarProber.MAX_FOLD_ROWS * _fold_cols(width)  # 88，点制预算上界
    partial = window - ToolbarProber.MAX_FOLD_ROWS * cols_all  # 下行前 4 点
    fold_clicks = ToolbarProber.MAX_FOLD_ROWS * cols_kept + partial - 2
    assert len(board.clicks) == fold_clicks + 2 + 3 * 2
    # 守卫性质：任何点击不得越过右缘 reserve（「✓ 确认」不可逆区）。
    assert all(x <= width - reserve for x, _y in board.clicks)
    assert all(not _inside(FarFoldBoard.FOLD, point) for point in board.clicks)
    # 预算按行而非按点：900/1920 宽下分别覆盖 4×33=132、4×69=276 个候选（上界），
    # 不再像固定 120 点那样在宽屏只覆盖不到两行。
    assert ToolbarProber.MAX_FOLD_ROWS * _fold_cols(900) == 132
    assert ToolbarProber.MAX_FOLD_ROWS * _fold_cols(1920) == 276


def test_fold_scan_skips_prescan_rejected_points():
    """撤销预扫描证伪过的候选点不得在折叠钮扫描里再点一次（省点击）。"""
    board = ToolbarBoard()
    board.panel_open = False
    prober, _ = _prober(board)
    prober._canvas = CANVAS
    prober._band = BAND
    prober._candidates = prober._make_candidates(board.image())
    prober._fold_limit = len(prober._candidates)
    prober._undo_tried = {(14, 456), (42, 456)}
    ordered = prober._fold_scan_candidates()
    assert (14, 456) not in ordered
    assert (42, 456) not in ordered
    prober._undo_tried = set()
    ordered_all = prober._fold_scan_candidates()
    assert (14, 456) in ordered_all and (42, 456) in ordered_all
    assert len(ordered_all) == len(ordered) + 2


def test_probe_restores_brush_when_cleanup_raises(monkeypatch):
    """清理抛错时仍要切回画笔，避免异常退出后停在橡皮态擦掉用户画作。"""
    board = ToolbarBoard()
    board.tool = "eraser"
    prober, _ = _prober(board)

    def boom(*_args, **_kwargs):
        raise ProbeError("E_PROBE_INK_UNCLEANABLE", "模拟清理失败")

    monkeypatch.setattr(prober, "_clear_probe_ink", boom)
    manual = _manual(
        fold=(312, 512),
        slider={"x": 100, "y": 530, "w": 200, "h": 12},
        undo=(130, 515),
        brush_selected_lum=180.0,
        brush_idle_lum=250.0,
    )

    with pytest.raises(ProbeError) as exc:
        prober.run(
            canvas=CANVAS,
            band=BAND,
            open_bitmap=manual,
            on_step=lambda _e: None,
            should_stop=lambda: False,
        )

    assert exc.value.code == "E_PROBE_INK_UNCLEANABLE"
    assert board.tool == "brush"


def test_probe_cleans_ink_when_probe_raises(monkeypatch):
    """I1：探测中途抛错时也要清掉刻度带测试线，且原始异常照常向外传播。

    正常清理路径首次抛 E_PROBE_NO_TOOLBAR；异常路径的 finally 必须再尽力清理
    一次，否则残留墨会污染用户画布与下一次重扫/标定。
    """
    board = ToolbarBoard()
    prober, _ = _prober(board)
    real_clear = prober._clear_probe_ink
    calls = {"n": 0}

    def fail_first(undo, check_stop):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ProbeError("E_PROBE_NO_TOOLBAR", "模拟清理前展开失败")
        return real_clear(undo, check_stop)

    monkeypatch.setattr(prober, "_clear_probe_ink", fail_first)

    with pytest.raises(ProbeError) as exc:
        prober.run(
            canvas=CANVAS,
            band=BAND,
            open_bitmap=None,
            on_step=lambda _e: None,
            should_stop=lambda: False,
        )

    assert exc.value.code == "E_PROBE_NO_TOOLBAR"
    assert calls["n"] == 2  # 正常清理一次失败 + 异常路径重试一次
    assert board.band_ink() == 0.0  # 带内墨迹回到基线
    assert board.tool == "brush"  # 工具态也恢复为画笔


def test_probe_stops_on_should_stop():
    board = ToolbarBoard()
    with pytest.raises(ProbeError) as exc:
        _run(board, should_stop=lambda: True)
    assert exc.value.code == "E_STOPPED"


def _collapsed_rect(rect) -> tuple[int, int, int, int]:
    x, y, w, h = rect
    return (x, y - ToolbarBoard.SHIFT, w, h)


def test_probe_without_rescan_has_no_collapsed():
    result = _run(ToolbarBoard())
    assert result.collapsed is None


def test_rescan_collapsed_produces_tools_collapsed():
    board = ToolbarBoard()
    steps: list[dict] = []
    result = _run(board, steps=steps, rescan_collapsed=True)
    # 收尾必须恢复展开态并把测试笔迹撤干净、工具切回画笔。
    assert board.panel_open
    assert board.band_ink() == 0.0
    assert board.tool == "brush"

    collapsed = result.collapsed
    assert collapsed is not None
    # 滑杆收起态被画布盖住，没有合法轨道，恒为 None。
    assert collapsed.slider is None
    assert collapsed.fold is not None and _inside(ToolbarBoard.FOLD, collapsed.fold)
    assert collapsed.undo is not None
    assert _inside(_collapsed_rect(ToolbarBoard.UNDO), collapsed.undo)
    assert collapsed.eraser is not None
    assert _inside(_collapsed_rect(ToolbarBoard.ERASER), collapsed.eraser)
    assert collapsed.brush is not None
    assert _inside(_collapsed_rect(ToolbarBoard.BRUSH), collapsed.brush)
    # 「x 不变、行平移」：收起态画笔与展开态同 x、y 上移了 SHIFT。
    assert collapsed.brush[0] == result.brush[0]
    assert result.brush[1] - collapsed.brush[1] > ToolbarProber.CANDIDATE_STEP // 2
    assert collapsed.brush_selected_lum is not None
    assert collapsed.brush_idle_lum is not None
    assert collapsed.canvas_rect == CANVAS
    assert _step_of(steps, "collapsed")["state"] == "done"


def test_prioritize_row_moves_anchor_row_first():
    points = [(14, 428), (42, 512), (14, 512), (70, 540), (14, 524)]
    ordered = ToolbarProber._prioritize_row(points, [516.0])
    # 锚点行 ±半格（502..530）内的候选排前、按行距升序；其余保持原序垫后。
    assert ordered == [(42, 512), (14, 512), (14, 524), (14, 428), (70, 540)]


def test_probe_undo_scan_starts_on_anchor_row():
    """折叠钮命中后，撤销扫描必须先扫同一工具行，而不是从最远的行重扫全网格。"""
    board = ToolbarBoard()
    _run(board)
    fold_click = next(
        i
        for i, point in enumerate(board.clicks)
        if _inside(ToolbarBoard.FOLD, point)
    )
    # 折叠命中后的下一次点击即撤销扫描的第一击：必须落在折叠钮所在行。
    undo_first = board.clicks[fold_click + 1]
    assert abs(undo_first[1] - 512.0) <= ToolbarProber.CANDIDATE_STEP // 2


def test_probe_stops_mid_scan():
    board = ToolbarBoard()
    calls = {"n": 0}

    def should_stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 3

    with pytest.raises(ProbeError) as exc:
        _run(board, should_stop=should_stop)
    assert exc.value.code == "E_STOPPED"
    assert board.clicks  # 已经开始了候选扫描
