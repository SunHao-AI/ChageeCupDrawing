"""工具栏行为探测：不依赖图标模板，靠「点击→观测」学出控件位置。

顺序：撤销 → 折叠钮 → 滑杆 → 橡皮/画笔。撤销最先：它的判据（画线→点击→
墨量跌落）不依赖滑杆轨道、也不依赖面板展开态，且撤销按钮在开合按钮上方
（真机结构先验），学到它就为折叠钮提供了行锚点。窄高窗口里画布与工具行
之间可隔着数百像素空白，「距画布固定几行」的盲扫够不着控件，撤销先行把
锚点建立起来，折叠/滑杆再按锚点定带。
所有点击落在画布外，墨迹观测只看刻度带。
画笔「选中/未选中」两态背景亮度差即选中指纹，供绘制期校验当前工具态。

「折叠」「展开」是外表不同、功能相反的两个独立按钮：折叠钮只在展开态可见，
展开钮只在收起态可见，各自只在一个状态下生效，且不能原位互逆。因此按钮命中
判据是「点击 → 轨道可见性达到期望方向」，不要求再点一次翻回来：展开态先扫
折叠钮（点击→轨道消失），需要重新展开时再扫展开钮（点击→轨道重现），学到
的展开钮记入 ``_learned_unfold`` 并作为收起态位图的 fold 字段入库——与执行
链「fold_closed 只用于展开面板」的用法天然对齐。

人工兜底字段优先级最高：非 None 的人工值直接采用；但落在画布内的人工点会被
丢弃并改走自动探测，避免任何探测点击污染画布。探测期间在刻度带画的测试笔迹
在返回前会用已识别的撤销按钮清空，并把工具态还原为画笔。

``on_step`` 结构化事件（{step, state, geometry, evidence}）把每个控件的探测
结果实时上报给校准页做标注与步骤流；state ∈ manual/done/failed。
折叠钮探测失败说明底部没有工具面板——画板多半停在初始态（只有 画笔/文字/
贴纸），此时按「最左列 + 像素差分门控 + 结构判据确认」三级安全约束尝试点
最左「画笔」进入绘画面板（brush_entry 步骤），误点右侧不可逆标签的风险为零。
结构判据（宽段轨道可见 / 撤销胶囊带出现内容）与折叠钮重探解耦：真机浅灰
轨道下重探必然失败，且盲扫面不得暴露给工具行最右的「✓ 确认」不可逆控件。
``rescan_collapsed=True`` 时在清理之后追加「收起面板→重扫」，产出收起态
工具位图（tools_collapsed）与收起态画布矩形；收起态重扫失败只上报 failed
步骤，不影响展开态探测成果。
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, replace
from typing import Callable, Protocol

import cv2
import numpy as np

from app.core.vision.canvas_detect import detect_canvas_rect
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
    # 展开态：本字段是「折叠」钮（展开态才可见/可点）；收起态位图里则是
    # 「展开」钮（收起态才可见/可点）——两态各存本态生效的按钮。
    fold: tuple[float, float] | None
    brush_selected_lum: float | None
    brush_idle_lum: float | None
    # 采集这套坐标那一刻的画布矩形（截图坐标系）：收起态面板会让画布扩大，
    # 两态坐标各绑自己的采集帧。仅收起态重扫产物填该字段（展开态为 None，
    # 保持旧数据形状兼容），供上层做「收起态是否真的换帧」的诊断与校验。
    canvas_rect: tuple[int, int, int, int] | None = None
    # 展开态探测的伴生结果：收起面板后重扫得到的收起态位图（仅
    # rescan_collapsed=True 时可能非 None）。持久化到 tools_open 前必须剥离。
    collapsed: "ToolBitmap | None" = None


# 结构化探测事件的步骤名，前端据此画对应标注与步骤流。
STEP_CANVAS = "canvas"  # 一键自动校准的画布自动识别（探测序列之前）
STEP_BRUSH_ENTRY = "brush_entry"  # 画板停在初始态时点最左「画笔」进入绘画面板
STEP_FOLD = "fold"
STEP_SLIDER = "slider"
STEP_UNDO = "undo"
STEP_ERASER = "eraser"
STEP_BRUSH = "brush"
STEP_FINGERPRINT = "fingerprint"
STEP_CLEANUP = "cleanup"
STEP_COLLAPSED = "collapsed"

STATE_MANUAL = "manual"  # 采用人工兜底/上次已知坐标，未重新探测
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"

STEP_LABELS = {
    STEP_CANVAS: "画布区域",
    STEP_BRUSH_ENTRY: "画笔入口",
    STEP_FOLD: "折叠按钮",
    STEP_SLIDER: "滑杆轨道",
    STEP_UNDO: "撤销按钮",
    STEP_ERASER: "橡皮按钮",
    STEP_BRUSH: "画笔按钮",
    STEP_FINGERPRINT: "画笔选中指纹",
    STEP_CLEANUP: "探测笔迹清理",
    STEP_COLLAPSED: "收起态重扫",
}


def _point_geometry(point: tuple[float, float] | None) -> dict | None:
    if point is None:
        return None
    return {"kind": "point", "x": round(float(point[0]), 1), "y": round(float(point[1]), 1)}


def _rect_geometry(rect: dict | None) -> dict | None:
    if rect is None:
        return None
    return {
        "kind": "rect",
        "x": int(rect["x"]),
        "y": int(rect["y"]),
        "w": int(rect["w"]),
        "h": int(rect["h"]),
    }


class ToolbarProber:
    """在画布下方工具条上做行为探测，产出各控件位置与画笔选中指纹。"""

    BAND_LINE_LEN = 46
    LUM_DELTA_MIN = 30  # 选中态判定最小亮度差
    CANDIDATE_STEP = 28  # 工具条候选点网格步长
    # 折叠钮扫描的候选行预算（仅在撤销锚点缺失时使用）。候选按行优先、距画布
    # 由近到远，截断等价于「只扫画布下方最近几行」，与截图宽度无关；按点数限
    # 死会让宽屏只覆盖不到两行。4 行 ≈ 画布下缘 112px：工具条通常紧贴画布下方
    # 70px 左右，3 行会漏掉折叠钮。更远的折叠钮在撤销锚点建立后由锚点扫描覆盖；
    # 无锚点时不自动扩大扫描面，避免无工具条应用在首绘承担全网格盲点的分钟级
    # worst case。
    MAX_FOLD_ROWS = 4
    # 内容行扫描预算：撤销/折叠锚点扫描只看「有内容的候选行」（邻域像素明显
    # 异于行背景），空白行一律不点。8 个内容行足以覆盖窄高窗口里画布与工具行
    # 之间数百像素的空白带（真机实测撤销行距画布底缘约 200px）。
    UNDO_SCAN_MAX_ROWS = 8
    # 内容行判定：邻域内与该行背景中位灰度差超过该值的像素数下限。JPEG 噪声
    # ±8、浅色控件（灰 240 的按钮底 vs 白背景）差 15，阈值 12 两者兼顾。
    CONTENT_ROW_DELTA = 12
    CONTENT_ROW_MIN_PX = 30
    # 撤销扫描右缘禁点区：绘画面板最右是「√ 确认提交」（不可逆），撤销胶囊
    # 恒在左侧。候选点在截图右缘 max(64px, 8%) 之外一律跳过。
    SCAN_RIGHT_RESERVE_PX = 64
    SCAN_RIGHT_RESERVE_RATIO = 0.08
    # 盲扫右缘危险格：内容行最右内容候选 x 超过窗口宽该比例即剔除。工具行
    # 最右恒为「✓ 确认」类不可逆控件，而行内按内容量降序预排会让粗黑的 ✓
    # 排在细灰开合钮之前被先点中，必须按结构先验剔除（真机 ✓ 在 ~93% 处，
    # 画笔/橡皮擦格约 30%/60%，最右单格被剔不影响整格命中）。
    DANGER_CELL_MIN_X_RATIO = 0.60
    # 撤销胶囊带：胶囊可浮在画布**内**左下角（展开态上移贴近画布中部），候选
    # 行从画布底缘向上取该行数的内容点、x 与预扫描安全区同规。带内只登记内容
    # 点（校准前置脏画布门禁保证画布为空，带内唯一内容源即胶囊），登记点获得
    # 画布内点击豁免；空白带不登记任何点，画布内点击硬约束对其余坐标不变。
    # 行数取 8（≈224px）：误点展开钮后面板重构、画布检测因近方形被拒时，旧
    # 收起矩形仍在，上移进画布中部的胶囊（实测距底缘 ~200px）必须还在带内，
    # 否则撤销成果在重建后永远找不回。
    CAPSULE_STRIP_ROWS = 8
    # 胶囊带行的横向展开宽度下限：画布左缘投影是 ~12px 宽、纵贯画布全高的
    # 暗列，单按邻域 ink 计数会被整列误登记（真机实测阴影列 ink 125~236，
    # 远超 30 门槛）。撤销胶囊整行（撤销+重做两图标）横占 ~60+ 列，24 列
    # 既滤掉纯阴影行又留足余量；行跨度按候选邻域中心对齐（±半格）统计。
    STRIP_ROW_MIN_COLS = 24
    # 撤销行镜像半区：撤销按钮恒在胶囊左半、右半是「重做」——盲扫点中重做
    # 会把刚撤销的探测笔迹原样补回，撤销/清理预算全部失真。撤销锚点建立后，
    # 同行内撤销点右邻 4 个网格步长（≈112px，真机胶囊半宽含图标 ~80px）的
    # 候选从一切盲扫路径剔除。
    UNDO_MIRROR_EXCLUDE_STEPS = 4
    # 探测器重建画布时的宽高比上限：面板展开态画布变矮近方形（真机实测
    # ratio≈0.80），默认档 0.78 直接拒检 → 重建失明、工具行/胶囊错位。
    # 只在探测期重建放宽，首帧/门禁检测保持默认严格档。
    CANVAS_MAX_RATIO = 0.85
    # 面板开合判据的画布底缘位移下限：点击折叠/展开钮会整体改动画布底缘
    # （真机展开↔收起约 22~30px），而工具格选中态高亮切换不动画布。滑杆轨道
    # 是上下文相关的（选中画笔/橡皮才出现），不可当开合态信号，改用画布底缘。
    PANEL_MOVE_MIN_PX = 10
    # 扫描期 UI 重构判据（画布下方区域像素差分）：面板开合/页面跳转会重写
    # 整个底部栏（万级像素），而工具格选中态高亮切换只有千级像素变化，
    # 不得误触重构重扫。
    RELAYOUT_MIN_PX = 5000
    RELAYOUT_MIN_RATIO = 0.03
    # 轨道结构 oracle 的段宽下限（窗口宽比）：真机轨道 ~84%；收起态工具格
    # （≤20%）、撤销胶囊阴影（~24%）都被该下限排除。初始态「提交创作」胶囊
    # （~37%）虽越线，但只出现在初始帧，折叠盲扫预算内的 4 行空白带会把
    # 误报消化在无害路径上。
    TRACK_WIDE_MIN_RATIO = 0.30
    # 初始态「画笔」入口试探预算：只扫底部最近 3 行的最左 3 列。画笔永远是
    # 最左标签，而误点右侧「文字/贴纸」不可逆，扫描面必须与右侧标签绝缘。
    BRUSH_ENTRY_MAX_ROWS = 3
    BRUSH_ENTRY_MAX_COLS = 3
    # 撤销判据：刻度带内墨量降到基线该比例以下即认定撤销生效。
    UNDO_DROP_RATIO = 0.4
    # 脏带逃逸的反应复验：画布带内叠有历史残留线时（历次探测失败留下的测试线
    # 与本次新线完全重合），撤销点击后带内墨量纹丝不动，纯墨量判据结构性失明
    # ——这正是「越失败越残留、越残留越失败」的死循环。兜底判据：点击撤销钮
    # 后其行带（撤销/重做图标点亮或置灰）出现小幅像素重构，且复点同行带再次
    # 反应（撤销钮每次都反馈；画笔/橡皮高亮等一次性切换不会二次反应），两轮
    # 复验认定撤销。反应量下限滤掉抗锯齿噪声，上限以上属面板重构、走 relayout
    # 重扫路径。仅对撤销结构先验区（画布左缘 + 两个网格步长之左）启用，与滑
    # 杆轨道绝缘。
    UNDO_REACTIVE_MIN_PX = 40
    INK_THRESHOLD = 160
    # 滑杆轨道的几何特征：连续暗段最小宽度、允许的行带高度。
    SLIDER_MIN_RUN = 60
    SLIDER_MAX_HEIGHT = 30
    SLIDER_MIN_HEIGHT = 2
    # 轨道宽松档（仅在撤销锚点建立后启用）：真机轨道比 strict 档（灰度<160）
    # 浅得多，改用「该行背景中位灰度 − 像素灰度 > DELTA」的相对判据；长度下限
    # 取窗口宽度的比例以排除撤销胶囊等短横线，上限排除画布下缘阴影带与卡片
    # 边框这类通宽长条。搜索带以撤销行上缘为顶，避开画布自身的投影带。
    SLIDER_RELAX_DELTA = 16
    SLIDER_RELAX_MIN_RUN_RATIO = 0.22
    SLIDER_RELAX_MAX_WIDTH_RATIO = 0.86
    SLIDER_ANCHOR_MARGIN_ROWS = 3

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
        # 各候选行的内容像素数（邻域与行背景中位差显著的像素），供行序提速。
        self._row_ink: dict[int, int] = {}
        # 「有内容」的候选行集合：撤销/折叠盲扫只走内容行（见 _content_candidates）。
        self._row_has_content: set[int] = set()
        # 每个候选点邻域的内容像素数（预扫描安全区按候选粒度筛内容点用）。
        self._candidate_ink: dict[tuple[int, int], int] = {}
        # 每个候选点邻域的内容质心（小按钮落在网格缝隙时点质心而非网格点）。
        self._candidate_centroid: dict[tuple[int, int], tuple[int, int]] = {}
        # 当前帧截图宽度（右缘 reserve 与滑杆长度比例用）。
        self._image_width: int = 0
        # 撤销按钮行 y：锚点后折叠钮扫描按「同行或下方」定带。只在当前帧有效——
        # 收起态重扫换帧后工具行会平移，须在 _rescan_collapsed 里清空。
        self._undo_row: float | None = None
        # 撤销按钮的可点坐标：镜像半区剔除的参照——同行内它右邻若干步长恒为
        # 「重做」，盲扫点中会把刚撤销的探测笔迹原样补回（见 _mirror_excluded）。
        self._undo_point: tuple[int, int] | None = None
        # 轨道宽松档的定带锚点：跨帧保留（收起帧里浅灰轨道同样要能检出），
        # 与只服务折叠扫描定带的 _undo_row 分开。
        self._slider_anchor: float | None = None
        # 撤销扫描已试过的候选点：预扫描空手后，折叠钮/撤销常规扫描不再重复
        # 盲点这些无反馈点（省点击，也让点击预算断言可精确复现）。
        self._undo_tried: set[tuple[int, int]] = set()
        # 折叠扫描的候选点数上限，由 run() 按截图宽度与 MAX_FOLD_ROWS 计算。
        self._fold_limit = 0
        # 探测前的刻度带墨像素数：清理只需回到这个深度，绝不越过它去撤用户笔画。
        self._band_baseline_px = 0
        # 探测自己画出且真正改变像素的笔数（与撤销历史一一对应），以及已消耗的撤销次数。
        self._probe_strokes = 0
        self._probe_undos = 0
        # 正常路径清理成功后置 True；异常/停止路径的 finally 据此避免重复撤销。
        self._ink_cleared = False
        # 探测期间学到的「展开」钮（收起态才生效）：供 _ensure_panel_open 优先
        # 点击，并作为收起态位图的 fold 字段（本态生效的按钮）入库。
        self._learned_unfold: tuple[float, float] | None = None
        # 撤销胶囊带（画布内左下角）：内容点列表与画布内点击豁免集合，随
        # _make_candidates 逐帧重算。空白带为空集，_click 的画布内硬约束不变。
        self._strip_candidates: list[tuple[int, int]] = []
        self._strip_points: set[tuple[int, int]] = set()
        # 人工兜底的折叠钮兼作展开钮候选（未知 UI 变体下可能两态同位）。
        self._manual_fold: tuple[float, float] | None = None
        # 工具格盲扫当前在找哪个（橡皮/画笔），仅供进度文案。
        self._tool_scan_label = "工具"
        self._on_step: Callable[[dict], None] = lambda _event: None
        # 调试：设置环境变量 CUP_DRAW_CALIB_DEBUG_DIR 后，每次点击/画线都会
        # 把操作后的窗口截图落盘到该目录，文件名带序号与当前步骤，供复盘。
        self._debug_dir = os.environ.get("CUP_DRAW_CALIB_DEBUG_DIR")
        self._debug_seq = 0
        self._debug_step = "init"

    def _snapshot(self, tag: str) -> None:
        """调试用：把当前帧按步骤分组落盘到 _debug_dir/{步骤}/{序号}_{标签}.png。

        文件名里的「步骤」即校准组件（canvas/undo/fold/slider/eraser/brush 等），
        方便按组件分组复盘每一步点击前后的窗口状态。调试目录优先取环境变量
        ``CUP_DRAW_CALIB_DEBUG_DIR``，否则读数据目录下的 ``calib_debug_dir.txt``
        （内容为一行目标路径），这样无需重启后端即可开关。
        """
        debug_dir = self._debug_dir_now()
        if not debug_dir:
            return
        try:
            image = self._grab()
            if image is None:
                return
            safe_step = str(self._debug_step).replace("/", "_").replace("\\", "_")
            safe_tag = str(tag).replace("/", "_").replace("\\", "_")
            subdir = os.path.join(debug_dir, safe_step)
            os.makedirs(subdir, exist_ok=True)
            path = os.path.join(subdir, f"{self._debug_seq:04d}_{safe_tag}.png")
            cv2.imwrite(path, image)
            self._debug_seq += 1
        except Exception:
            # 调试落盘失败不得影响校准主流程。
            pass

    @staticmethod
    def _debug_dir_now() -> str | None:
        """运行时解析调试目录：环境变量优先，其次数据目录 marker 文件。"""
        env = os.environ.get("CUP_DRAW_CALIB_DEBUG_DIR")
        if env:
            return env
        try:
            from app.config import default_data_dir

            marker = default_data_dir() / "calib_debug_dir.txt"
            value = marker.read_text(encoding="utf-8").strip()
            return value or None
        except Exception:
            return None

    def _step(
        self,
        name: str,
        state: str,
        geometry: dict | None = None,
        evidence: str = "",
    ) -> None:
        self._debug_step = name
        self._on_step({
            "step": name,
            "state": state,
            "geometry": geometry,
            "evidence": evidence,
        })

    def _progress(self, name: str, message: str) -> None:
        """进行中的可读进度（同一步骤会持续覆盖前一条）。"""
        self._step(name, STATE_RUNNING, None, message)

    # ---------- 对外主流程 ----------

    def run(
        self,
        *,
        canvas: tuple[int, int, int, int],
        band: tuple[int, int, int, int],
        open_bitmap: "ToolBitmap | None",
        on_step: Callable[[dict], None],
        should_stop: Callable[[], bool],
        rescan_collapsed: bool = False,
        undo_prescan: bool = True,
    ) -> ToolBitmap:
        self._canvas = tuple(int(v) for v in canvas)
        self._band = tuple[int, int, int, int]((int(v) for v in band))
        self._on_step = on_step

        manual = open_bitmap
        step = self._step

        fold = self._clean_point(manual.fold if manual else None)
        self._manual_fold = fold
        self._learned_unfold = None
        slider = self._clean_slider(manual.slider if manual else None)
        undo = self._clean_point(manual.undo if manual else None)
        eraser = self._clean_point(manual.eraser if manual else None)
        brush = self._clean_point(manual.brush if manual else None)
        # 人工兜底/上次探测已知坐标的「工具行」：用于候选排序先验。工具格、
        # 撤销与滑杆几乎总在同一行；优先扫这一行可把扫描从全网格降到单行。
        manual_rows = [
            point[1]
            for point in (undo, eraser, brush)
            if point is not None
        ]
        selected_lum = manual.brush_selected_lum if manual else None
        idle_lum = manual.brush_idle_lum if manual else None
        slider_manual = slider is not None

        def check_stop() -> None:
            if should_stop():
                raise ProbeError("E_STOPPED", "工具栏探测被停止")

        check_stop()
        first_image = self._grab()
        self._snapshot("start")
        self._candidates = self._make_candidates(first_image)
        cols = max(1, math.ceil(first_image.shape[1] / self.CANDIDATE_STEP))
        self._fold_limit = min(len(self._candidates), self.MAX_FOLD_ROWS * cols)
        self._band_baseline_px = self._band_ink_pixels(first_image)
        self._probe_strokes = 0
        self._probe_undos = 0
        self._ink_cleared = False
        self._undo_row = undo[1] if undo is not None else None
        self._undo_point = (int(undo[0]), int(undo[1])) if undo is not None else None
        self._slider_anchor = self._undo_row
        self._undo_tried = set()

        # 撤销预扫描（安全区=画布左缘之左的内容点，见 _prescan_candidates）：
        # 判据不依赖滑杆轨道与面板展开态，先把撤销学到手并建立行锚点——
        # 折叠钮扫描按锚点定带、滑杆检测启用宽松档。真机窄高窗口里「画布下缘
        # 数 4 行」够不着任何控件，这条预扫描是锚点的主来源；初始态画板筛不出
        # 候选（标签全在画布左缘以右），预扫描空转即退，点击面与旧实现一致。
        undo_prescan_hit = False
        # 结构确认已进入绘画面板（即便开合钮/撤销都没学到）：撤销预扫描误点
        # 画笔标签或 brush_entry 结构判据通过时置真，滑杆缺失时据此降级而非
        # 整链报废。
        panel_entered = False
        if undo is None and undo_prescan:
            prescan_area = self._prescan_candidates()
            if prescan_area:
                undo = self._probe_undo(
                    fold, slider, eraser, brush, check_stop,
                    ordered=prescan_area,
                    allowed=prescan_area,
                )
            if undo is not None:
                undo_prescan_hit = True
                step(
                    STEP_UNDO, STATE_DONE, _point_geometry(undo),
                    "画布左侧扫描先行识别（为开合钮提供行锚点）",
                )
            elif self._below_canvas_changed(first_image, self._grab(), 150, 0.0005):
                # 预扫描误点了初始态最左「画笔」标签（安全区设计内的可接受
                # 副作用）：已进入绘画面板，但候选网格还是初始帧的——标签行
                # 内容量最高会被后续折叠钮扫描排到最前并横向扫进「文字/贴纸」
                # 列。按新帧重算网格，让标签行从扫描序里消失。
                first_image = self._grab()
                self._candidates = self._make_candidates(first_image)
                self._undo_tried = set()
                cols = max(1, math.ceil(first_image.shape[1] / self.CANDIDATE_STEP))
                self._fold_limit = min(
                    len(self._candidates), self.MAX_FOLD_ROWS * cols
                )
                panel_entered = True
                # 宽松档滑杆定带锚点：工具行是面板态最靠画布的内容行。
                if self._slider_anchor is None and self._row_has_content:
                    self._slider_anchor = float(min(self._row_has_content))
                step(
                    STEP_BRUSH_ENTRY, STATE_DONE, None,
                    "撤销预扫描期间底部栏重构（疑经最左画笔标签进入绘画面板），"
                    "已按新帧重算候选",
                )

        if fold is None:
            skip = self._skip_points(slider=slider, undo=undo, eraser=eraser, brush=brush)
            fold = self._probe_fold_pair(skip, check_stop)
            if fold is None and not undo_prescan_hit and not panel_entered:
                # 折叠钮学不到且撤销也没找到 ⇔ 底部没有工具面板：画板大概率
                # 停在初始态（只有 画笔/文字/贴纸）。先尝试点最左「画笔」进入
                # 绘画面板，成功时重探已顺带学到折叠钮。撤销已学到说明工具区
                # 就在下方（只是轨道判据没认出来），不去盲点标签行。
                fold, entered = self._try_brush_entry(
                    skip,
                    lambda blocked: self._probe_fold_pair(blocked, check_stop),
                    check_stop,
                )
                panel_entered = panel_entered or entered
            if fold is not None:
                step(STEP_FOLD, STATE_DONE, _point_geometry(fold))
            else:
                step(
                    STEP_FOLD, STATE_FAILED, None,
                    "开合钮未学到（撤销锚点下方仍无有效开合控件），请人工补标"
                    if undo is not None else "",
                )
        else:
            step(STEP_FOLD, STATE_MANUAL, _point_geometry(fold), "人工/上次校准坐标")

        if slider is None:
            opened = self._ensure_panel_open(fold, check_stop)
            if opened or undo is not None:
                # 展开态确认失败但撤销已在手：锚点定带 + 宽松判据仍可定位轨道
                # （真机轨道浅灰，strict 恒 False，轨道可见性 oracle 已不可用）。
                slider = self._locate_slider(self._grab())
            if slider is not None:
                step(STEP_SLIDER, STATE_DONE, _rect_geometry(slider))
            elif undo is not None:
                # 撤销已在手：滑杆缺失只降级自动变粗（回退单细笔），不值得把
                # 撤销/清理能力连同全部探测成果一起作废。面板状态未知时宁可
                # 挂起也不要在错位坐标上盲点。
                step(
                    STEP_SLIDER, STATE_FAILED, None,
                    "轨道不可检测且面板展开态无法确认，请人工框选滑杆",
                )
            elif fold is None and not panel_entered:
                step(STEP_SLIDER, STATE_FAILED, None, "未探到工具条")
                raise ProbeError(
                    "E_PROBE_NO_TOOLBAR", "工具条区域未探到任何控件（请在校准页人工补标）"
                )
            elif fold is None:
                # 已确认进入绘画面板（结构判据）但轨道定位失败：与撤销降级同理，
                # 滑杆缺失只降级自动变粗，撤销/工具格探测成果继续保留。
                step(
                    STEP_SLIDER, STATE_FAILED, None,
                    "已进入绘画面板但未定位滑杆轨道，请人工框选滑杆",
                )
            else:
                step(STEP_SLIDER, STATE_FAILED, None, "面板展开态下未找到轨道")
                raise ProbeError(
                    "E_PROBE_NO_SLIDER", "面板展开态下未找到笔宽滑杆轨道（请人工框选滑杆）"
                )
        else:
            step(STEP_SLIDER, STATE_MANUAL, _rect_geometry(slider), "人工/上次校准坐标")

        # 结构先验：撤销/画笔/橡皮与折叠钮、滑杆几乎总在同一条工具行上。已识别
        # 控件所在行（±半格）的候选排到最前，其余按原序垫后——后续扫描（工具格）
        # 在常见布局下只需扫一行，把最坏路径从「全网格·分钟级」压到「单行·秒级」，
        # 同时缩小观测失效时的空耗面。
        anchors = [fold[1]] if fold is not None else []
        if slider is not None:
            anchors.append(slider["y"] + slider["h"] / 2.0)
        anchors += manual_rows
        if anchors:
            self._candidates = self._prioritize_row(self._candidates, anchors)

        if undo is None:
            undo = self._probe_undo(fold, slider, eraser, brush, check_stop)
            if undo is not None:
                step(STEP_UNDO, STATE_DONE, _point_geometry(undo))
            else:
                step(STEP_UNDO, STATE_FAILED)
        elif not undo_prescan_hit:
            step(STEP_UNDO, STATE_MANUAL, _point_geometry(undo), "人工/上次校准坐标")

        tools_probed = eraser is None or brush is None
        if tools_probed:
            try:
                self._require_panel_open(fold, check_stop, assume_open=slider_manual)
            except ProbeError:
                if undo is None:
                    raise
                # 撤销在手而面板开合失效：工具格坐标必须在展开态采集，跳过探测
                # 但保留撤销成果收尾——清理照常，橡皮/画笔引导人工补标。
                evidence = "面板展开态无法确认，请人工补标开合钮后重试"
                step(STEP_ERASER, STATE_FAILED, None, evidence)
                step(STEP_BRUSH, STATE_FAILED, None, evidence)
                tools_probed = False
            else:
                got_eraser, got_brush = self._probe_tools(
                    fold, slider, undo, eraser, brush, check_stop
                )
                if eraser is None and got_eraser is not None:
                    eraser = got_eraser
                    step(STEP_ERASER, STATE_DONE, _point_geometry(eraser))
                if brush is None and got_brush is not None:
                    brush = got_brush
                    step(STEP_BRUSH, STATE_DONE, _point_geometry(brush))
                if eraser is None:
                    step(STEP_ERASER, STATE_FAILED)
                if brush is None:
                    step(STEP_BRUSH, STATE_FAILED)
        else:
            step(STEP_ERASER, STATE_MANUAL, _point_geometry(eraser), "人工/上次校准坐标")
            step(STEP_BRUSH, STATE_MANUAL, _point_geometry(brush), "人工/上次校准坐标")

        if (
            brush is not None
            and eraser is not None
            and (selected_lum is None or idle_lum is None)
        ):
            selected_lum, idle_lum = self._fingerprint(
                brush, eraser, fold, selected_lum, idle_lum, check_stop,
                assume_open=slider_manual,
            )
        if selected_lum is not None and idle_lum is not None:
            step(
                STEP_FINGERPRINT, STATE_DONE, _point_geometry(brush),
                f"选中亮度 {selected_lum:.0f} / 未选中 {idle_lum:.0f}",
            )
        elif selected_lum is not None or idle_lum is not None:
            step(
                STEP_FINGERPRINT, STATE_FAILED, None,
                "亮度差不足，仅部分指纹可用（跳过绘制期画笔校验）",
            )

        # 清理探测笔迹：残留墨会落进 pen_calibration 的首个测宽窗口。
        try:
            self._require_panel_open(fold, check_stop, assume_open=slider_manual)
            self._clear_probe_ink(undo, check_stop)
            self._ink_cleared = True
            step(STEP_CLEANUP, STATE_DONE)
            open_result = ToolBitmap(
                brush=brush,
                eraser=eraser,
                slider=dict(slider) if slider is not None else None,
                undo=undo,
                fold=fold,
                brush_selected_lum=selected_lum,
                brush_idle_lum=idle_lum,
            )
            collapsed = None
            if rescan_collapsed:
                collapsed = self._rescan_collapsed(fold, open_result, check_stop)
            if tools_probed and brush is not None:
                check_stop()
            return replace(open_result, collapsed=collapsed)
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

    # ---------- 7) 收起态重扫（tools_collapsed 自动产出） ----------

    def _rescan_collapsed(
        self,
        fold: tuple[float, float] | None,
        open_result: "ToolBitmap",
        check_stop: Callable[[], None],
    ) -> "ToolBitmap | None":
        """收起面板后按「x 不变、行平移」重扫，产出收起态工具位图。

        工具条控件两态 x 坐标一致，只有工具行 y 整体平移，因此不再全网格盲扫：
        对展开态已验证的 undo/eraser/brush x 逐个试少量候选行，命中判据与展开态
        语义探测完全相同。收起后画布扩大吞掉滑杆轨道，点击守卫改用新帧画布。
        一切未通过语义验证的项都留 None——绝不跨态猜坐标。失败只报 failed 步骤
        并返回 None，展开态成果不受影响；收尾尽力恢复展开态并用展开态撤销清理
        重扫期间画的测试线。
        """
        self._ink_cleared = False
        reopened = False
        print(
            f"[DBG-RESCAN] start strokes={self._probe_strokes} undos={self._probe_undos} "
            f"ink={self._band_ink_pixels()} band={self._band}",
            flush=True,
        )
        try:
            if not self._set_panel_visible([fold], want_visible=False, check_stop=check_stop):
                self._step(STEP_COLLAPSED, STATE_FAILED, None, "面板无法收起（折叠点击无效）")
                return None

            image = self._grab()
            found = detect_canvas_rect(image, max_ratio=self.CANVAS_MAX_RATIO)
            if found is not None:
                left, top, right, bottom = found
                self._canvas = (left, top, right - left, bottom - top)
            self._candidates = self._make_candidates(image)
            cols = max(1, math.ceil(image.shape[1] / self.CANDIDATE_STEP))
            self._fold_limit = min(len(self._candidates), self.MAX_FOLD_ROWS * cols)
            # 换帧：展开态撤销行 y 对收起帧无意义（工具行整体平移），折叠扫描
            # 定带锚点与旧帧证伪点集合一并作废，回退行数预算；轨道宽松档锚点
            # 跨帧保留。
            self._undo_row = None
            self._undo_point = None
            self._undo_tried = set()
            rows = self._transfer_rows()

            skip = self._skip_points(
                fold=open_result.fold, slider=open_result.slider,
                undo=open_result.undo, eraser=open_result.eraser, brush=open_result.brush,
            )
            # 收起态里生效的是「展开」钮。先试可逆 UI（展开态钮原位仍可展开），
            # 不成再在收起帧按「点击→轨道重现」方向扫描学出展开钮。
            unfold_point = self._unfold_same_place(open_result.fold, check_stop)
            if unfold_point is None:
                skip |= self._excluded_grid([open_result.fold])
                # 收起帧里工具行整体平移到距画布数行之外（真机实测 ~250px，
                # 4 行折叠预算够不着）。此刻面板态已确认——收起的是绘画面板
                # 而非初始态标签行，内容行扫描面是安全的：收起帧内容行只有
                # 撤销胶囊行与工具行，右缘守卫继续挡「✓ 确认」。
                unfold_point = self._scan_button(
                    skip, want_visible=True, check_stop=check_stop,
                    first=self._learned_unfold,
                    ordered=self._right_edge_guard(
                        self._content_candidates(limit_rows=self.UNDO_SCAN_MAX_ROWS)
                    ),
                )
                if unfold_point is not None and self._panel_open():
                    # 命中点击把面板带回了展开态；工具行平移必须在收起帧做，
                    # 用已知有效的展开态折叠钮放回去。
                    if not self._set_panel_visible(
                        [open_result.fold], want_visible=False, check_stop=check_stop
                    ):
                        self._step(
                            STEP_COLLAPSED, STATE_FAILED, None,
                            "展开钮已学到但面板无法放回收起态",
                        )
                        reopened = True  # 面板就在展开态，salvage 只需清理
                        self._clear_probe_ink(open_result.undo, check_stop)
                        self._ink_cleared = True
                        return None
            if unfold_point is None:
                self._step(STEP_COLLAPSED, STATE_FAILED, None, "收起帧未找到展开按钮")
                reopened = self._set_panel_visible(
                    [open_result.fold], want_visible=True, check_stop=lambda: None
                )
                return None
            self._learned_unfold = unfold_point
            # 收起态位图的 fold 存「本态生效的按钮」= 展开钮，执行链直接拿它开面板。
            fold_closed = unfold_point

            exclude = [p for p in (fold_closed, open_result.fold) if p is not None]
            undo_closed = None
            if open_result.undo is not None:
                undo_closed = self._transfer_undo(open_result.undo, exclude, check_stop)
                if undo_closed is not None:
                    exclude.append(undo_closed)
            eraser_closed = self._transfer_tool(
                open_result.eraser, rows, exclude, check_stop, want_ink=False
            )
            brush_closed = self._transfer_tool(
                open_result.brush, rows, exclude, check_stop, want_ink=True
            )
            print(
                f"[DBG-RESCAN] transfers done strokes={self._probe_strokes} "
                f"undos={self._probe_undos} ink={self._band_ink_pixels()} "
                f"undo_closed={undo_closed} eraser_closed={eraser_closed} "
                f"brush_closed={brush_closed}",
                flush=True,
            )
            if brush_closed is not None:
                exclude.append(brush_closed)
            selected_c = idle_c = None
            if brush_closed is not None and eraser_closed is not None:
                selected_c, idle_c = self._fingerprint_closed(
                    brush_closed, eraser_closed, check_stop
                )

            # 先恢复展开态再清理：展开态撤销坐标经过完整验证，最可靠。无法展开
            # （折叠钮漂移等）才退回收起帧学到的 undo_closed。
            reopened = self._set_panel_visible(
                [fold_closed, open_result.fold], want_visible=True, check_stop=check_stop
            )
            self._clear_probe_ink(
                open_result.undo if reopened else undo_closed, check_stop
            )
            self._ink_cleared = True
            if not reopened:
                self._step(
                    STEP_COLLAPSED, STATE_DONE,
                    _rect_geometry({
                        "x": self._canvas[0], "y": self._canvas[1],
                        "w": self._canvas[2], "h": self._canvas[3],
                    }),
                    "收起态位图已产出，但面板无法恢复展开（请检查折叠按钮）",
                )
                return None
            self._step(
                STEP_COLLAPSED, STATE_DONE,
                _point_geometry(fold_closed),
                self._collapsed_evidence(
                    fold_closed, undo_closed, eraser_closed, brush_closed,
                    selected_c, idle_c,
                ),
            )
            return ToolBitmap(
                brush=brush_closed,
                eraser=eraser_closed,
                # 滑杆收起态不可见（被扩大的画布盖住），没有合法的收起态轨道。
                slider=None,
                undo=undo_closed,
                fold=fold_closed,
                brush_selected_lum=selected_c,
                brush_idle_lum=idle_c,
                canvas_rect=self._canvas,
            )
        except ProbeError as exc:
            if exc.code == "E_STOPPED":
                raise
            self._step(STEP_COLLAPSED, STATE_FAILED, None, exc.message)
            return None
        except Exception as exc:
            self._step(
                STEP_COLLAPSED, STATE_FAILED, None,
                f"收起态重扫异常（{exc.__class__.__name__}）",
            )
            return None
        finally:
            # 一切未干净收尾的路径（失败/异常/停止）统一尽力恢复展开态并清理；
            # 正常收尾路径已把 reopened 置 True 或显式清理过。
            if not reopened:
                self._collapsed_salvage(open_result)

    def _collapsed_salvage(self, open_result: "ToolBitmap") -> None:
        """收起态重扫失败路径：尽力恢复展开态并清理测试线，吞掉一切异常。"""
        try:
            self._set_panel_visible(
                [open_result.fold], want_visible=True, check_stop=lambda: None
            )
            self._clear_probe_ink(open_result.undo, lambda: None)
            self._ink_cleared = True
        except Exception:
            pass

    @staticmethod
    def _collapsed_evidence(
        fold: tuple[float, float] | None,
        undo: tuple[float, float] | None,
        eraser: tuple[float, float] | None,
        brush: tuple[float, float] | None,
        selected_lum: float | None,
        idle_lum: float | None,
    ) -> str:
        found = [
            label
            for label, value in (("展开", fold), ("撤销", undo), ("橡皮", eraser), ("画笔", brush))
            if value is not None
        ]
        fingerprint = "，含画笔指纹" if selected_lum is not None and idle_lum is not None else ""
        return "收起态已学到：" + ("、".join(found) or "无") + fingerprint

    def _canvas_bottom(self) -> int:
        """当前画布底缘 y（`_canvas` 以 xywh 存储）。"""
        return int(self._canvas[1] + self._canvas[3])

    def _recanvased_bottom(self, image) -> int | None:
        """重检 ``image`` 的画布并更新 `_canvas`，返回新底缘；检不到返回 None。"""
        found = detect_canvas_rect(image, max_ratio=self.CANVAS_MAX_RATIO)
        if found is None:
            return None
        left, top, right, bottom = found
        self._canvas = (left, top, right - left, bottom - top)
        return bottom

    def _panel_flipped(
        self, before_bottom: int, after_bottom: int, want_open: bool
    ) -> bool:
        """面板开合判据：折叠（want_open=False）画布底缘下移、展开（True）上移。

        滑杆轨道是上下文相关的（选中画笔/橡皮才出现），不能当开合态信号；
        画布底缘随开合整体位移约 22~30px，而工具格选中态高亮切换不动画布，
        是更可靠的判据。
        """
        delta = after_bottom - before_bottom
        if want_open:
            return delta <= -self.PANEL_MOVE_MIN_PX
        return delta >= self.PANEL_MOVE_MIN_PX

    def _panel_flipped_full(
        self, before_frame, before_bottom: int, after_frame, want_open: bool
    ) -> bool:
        """开合判据：滑杆可见性翻转（快）优先，画布底缘位移（慢）兜底。

        滑杆翻转用行扫描即可判断，合成板/轨道不上下文相关的真机走这条快路径；
        真机展开态未选中画笔/橡皮时滑杆本就不可见、点击折叠钮后仍不可见（无
        翻转），此时才走画布底缘位移判据（需 detect_canvas_rect，较慢）。
        """
        before_track = self._track_visible_on(before_frame)
        after_track = self._track_visible_on(after_frame)
        if before_track != after_track and after_track == want_open:
            return True
        # 画布下方无大重构就不必 detect_canvas_rect（慢）：误点工具格只改选中
        # 态、折叠/展开才整体平移底部栏。
        if not self._below_canvas_changed(
            before_frame, after_frame, self.RELAYOUT_MIN_PX, self.RELAYOUT_MIN_RATIO
        ):
            return False
        after_bottom = self._recanvased_bottom(after_frame)
        return after_bottom is not None and self._panel_flipped(
            before_bottom, after_bottom, want_open
        )

    def _panel_open(self) -> bool:
        """面板是否展开：滑杆可见优先，撤销胶囊浮在画布内兜底。

        真机展开态可能未选中画笔/橡皮（滑杆上下文相关、暂不可见），此时用
        「撤销胶囊是否浮在画布内」兜底——展开态撤销胶囊横跨画布底缘、收起态
        在画布下方。合成板用滑杆可见性模拟展开态，优先命中滑杆分支即可。
        """
        if self._track_visible():
            return True
        if self._undo_row is not None:
            return self._undo_row < self._canvas_bottom()
        return False

    def _track_visible(self) -> bool:
        # strict 档（绝对暗度 <160）对真机浅灰轨道（230~240）恒失明——上一轮
        # 真机校准全链失败即源于此。宽段结构判据作为第二 oracle 兜底；
        # ``_find_slider`` 本身保持纯 strict（收起态工具格不得被当轨道）。
        return self._track_visible_on(self._grab())

    def _track_visible_on(self, image) -> bool:
        """对给定图判断滑杆轨道是否可见（不重新截图）。"""
        if self._find_slider(image) is not None:
            return True
        return self._wide_track_visible(image)

    def _wide_track_visible(self, image) -> bool:
        """轨道可见性的结构兜底：画布下方存在宽度占比 [30%, 86%] 的相对暗段行带。

        与宽松档同为「带背景中位灰度 − 像素灰度」相对判据，但段宽下限提到
        30%：真机收起态工具格（≤20% 宽）、撤销胶囊阴影（~24%）都不越线，
        不会重蹈「宽松档接入 _track_visible 导致收起态恒真」的覆辙。初始态
        「提交创作」胶囊（~37%）虽满足，但只出现在初始帧——彼时折叠盲扫
        预算只覆盖画布下 4 行空白带，误报消化在无害路径。判据只有行带计数，
        不做精确定位（定位仍归 ``_locate_slider``）。
        """
        gray = image[..., 0] if image.ndim == 3 else image
        height, width = gray.shape
        top = min(height, self._canvas[1] + self._canvas[3] + 8)
        if top >= height:
            return False
        region = gray[top:height, :]
        if region.size == 0:
            return False
        background = int(np.median(region))
        min_run = int(width * self.TRACK_WIDE_MIN_RATIO)
        max_run = int(width * self.SLIDER_RELAX_MAX_WIDTH_RATIO)
        consecutive = 0
        for row in range(region.shape[0]):
            dark = (background - region[row].astype(np.int16)) > self.SLIDER_RELAX_DELTA
            run = self._longest_run(dark)
            if min_run < run < max_run:
                consecutive += 1
                if consecutive >= self.SLIDER_MIN_HEIGHT:
                    return True
            else:
                consecutive = 0
        return False

    def _set_panel_visible(
        self,
        points: list["tuple[float, float] | None"],
        *,
        want_visible: bool,
        check_stop: Callable[[], None],
    ) -> bool:
        """逐个候选按钮坐标点击，直到面板开合态与期望一致；全部无效返回 False。"""
        candidates = [p for p in points if p is not None]
        if self._panel_open() == want_visible:
            return True
        for _ in range(2):
            for point in candidates:
                try:
                    check_stop()
                    self._click(float(point[0]), float(point[1]))
                except ProbeError as exc:
                    if exc.code == "E_PROBE_CLICK_IN_CANVAS":
                        continue
                    raise
                self._sleep(self._settle_s)
                if self._panel_open() == want_visible:
                    return True
        return self._panel_open() == want_visible

    def _unfold_same_place(
        self,
        open_fold: tuple[float, float] | None,
        check_stop: Callable[[], None],
    ) -> tuple[float, float] | None:
        """可逆 UI（单钮两态互切）验证：收起态点展开态钮原位，面板应展开。

        再点一次须再次收起（把面板放回收起态，后续工具行平移仍在收起帧做）。
        任何一步不符都不算数，交回收起帧的展开钮方向扫描。
        """
        if open_fold is None:
            return None
        if self._panel_open():
            return None
        try:
            check_stop()
            self._click(float(open_fold[0]), float(open_fold[1]))
        except ProbeError as exc:
            if exc.code == "E_PROBE_CLICK_IN_CANVAS":
                return None
            raise
        self._sleep(self._settle_s)
        if not self._panel_open():
            return None
        self._click(float(open_fold[0]), float(open_fold[1]))
        self._sleep(self._settle_s)
        if not self._panel_open():
            return (float(open_fold[0]), float(open_fold[1]))
        return None

    def _scan_button(
        self,
        skip: set[tuple[int, int]],
        *,
        want_visible: bool,
        check_stop: Callable[[], None],
        first: "tuple[float, float] | None" = None,
        ordered: "list[tuple[int, int]] | None" = None,
    ) -> tuple[float, float] | None:
        """在当前态扫候选：点击后画布底缘按 want_visible 方向位移者即命中。

        不要求原位可逆——「折叠」「展开」本就是两个按钮。``first`` 优先试
        （可逆 UI 的同位按钮）；未命中则跳过其网格邻域，避免在同一按钮区
        域重复点击。``ordered`` 覆盖默认扫描序（`_fold_scan_candidates`）：
        收起态重扫在已确认的面板态下运行，工具行整体平移到 4 行预算之外，
        由调用方传入内容行扫描面。

        判据两级：①画布底缘位移（真机展开/收起约 22~30px，滑杆是上下文相关
        的不可靠信号）；②滑杆轨道可见性翻转（合成板/轨道不上下文相关的真机
        兜底——要求点击前后可见性确实翻转，避免「展开态本就无滑杆」时误命中）。
        """

        def flipped(before_frame, before_bottom, after_frame) -> bool:
            return self._panel_flipped_full(
                before_frame, before_bottom, after_frame, want_visible
            )

        if first is not None:
            before_frame = self._grab()
            before = self._band_ink_pixels(before_frame)
            before_bottom = self._canvas_bottom()
            try:
                check_stop()
                self._click(float(first[0]), float(first[1]))
                self._sleep(self._settle_s)
            except ProbeError as exc:
                if exc.code != "E_PROBE_CLICK_IN_CANVAS":
                    raise
            else:
                self._note_undo_like_drop(before)
                if flipped(before_frame, before_bottom, self._grab()):
                    return (float(first[0]), float(first[1]))
            skip = skip | self._excluded_grid([first])
        if ordered is None:
            ordered = self._fold_scan_candidates()
        self._progress(
            STEP_FOLD,
            f"正在识别工具面板开合钮（已试 {len(skip)} 个候选，共 {len(ordered)} 个）",
        )
        for scanned, point in enumerate(ordered, start=1):
            if point in skip:
                continue
            if scanned % 4 == 1:
                self._progress(
                    STEP_FOLD,
                    f"正在识别工具面板开合钮（已试 {scanned}/{len(ordered)} 个候选）",
                )
            check_stop()
            before_frame = self._grab()
            before = self._band_ink_pixels(before_frame)
            before_bottom = self._canvas_bottom()
            target = self._click_target(point)
            try:
                self._click(float(target[0]), float(target[1]))
            except ProbeError as exc:
                if exc.code == "E_PROBE_CLICK_IN_CANVAS":
                    continue
                raise
            self._sleep(self._settle_s)
            self._note_undo_like_drop(before)
            if flipped(before_frame, before_bottom, self._grab()):
                return target
        return None

    def _click_target(self, point: tuple[int, int]) -> tuple[float, float]:
        """候选点 → 实际点击坐标：优先邻域内容质心，落在网格缝隙的小按钮也能点中。"""
        target = self._candidate_centroid.get(point)
        if target is None:
            return (float(point[0]), float(point[1]))
        return (float(target[0]), float(target[1]))

    def _round_trip(
        self,
        fold: tuple[float, float],
        unfold: tuple[float, float],
        check_stop: Callable[[], None],
    ) -> bool:
        """成对复验：折叠钮点击⇒收起、展开钮点击⇒展开，收尾停在展开态。

        「关一次就不再开」的干扰控件过不了第二轮；可逆单钮（fold==unfold）
        同样成立。判据用画布底缘位移（滑杆轨道上下文相关，仅作兜底）。
        """

        def flip(point: tuple[float, float], want_open: bool) -> bool:
            before_frame = self._grab()
            before_bottom = self._canvas_bottom()
            check_stop()
            self._click(float(point[0]), float(point[1]))
            self._sleep(self._settle_s)
            return self._panel_flipped_full(
                before_frame, before_bottom, self._grab(), want_open
            )

        if not self._panel_open():
            if not flip(unfold, want_open=True):
                return False
        if not flip(fold, want_open=False):
            return False
        return flip(unfold, want_open=True)

    def _refresh_frame(self) -> None:
        """面板开合/页面重构后重读画布与候选网格，丢弃旧帧锚点与证伪点。"""
        image = self._grab()
        found = detect_canvas_rect(image, max_ratio=self.CANVAS_MAX_RATIO)
        if found is not None:
            left, top, right, bottom = found
            self._canvas = (left, top, right - left, bottom - top)
        self._candidates = self._make_candidates(image)
        cols = max(1, math.ceil(image.shape[1] / self.CANDIDATE_STEP))
        self._fold_limit = min(len(self._candidates), self.MAX_FOLD_ROWS * cols)
        self._undo_row = None
        self._undo_point = None
        self._undo_tried = set()

    def _learn_fold_pair_once(
        self, skip: set[tuple[int, int]], check_stop: Callable[[], None]
    ) -> tuple["tuple[float, float] | None", "tuple[float, float] | None"]:
        """按当前态学习一对按钮：(折叠钮, 展开钮)，任一方向都支持先学哪个。

        展开态先扫折叠钮（点击→画布底缘下移），再扫展开钮（点击→画布底缘
        上移）；收起态反之。第二次扫描把第一次命中的钮网格邻域并入 skip，但
        先原位试一次——可逆单钮 UI 的展开钮就在折叠钮原位。

        折叠/展开后工具行会整体平移（真机/PC 收起后工具行距画布 ~200px），
        展开态候选网格够不着收起态展开钮。因此第一次命中后刷新帧，再用内容
        行扫描面（收起帧内容行只有撤销胶囊行与工具行，右缘守卫继续挡「✓」）
        去学第二个钮。
        """
        if self._panel_open():
            fold = self._scan_button(skip, want_visible=False, check_stop=check_stop)
            if fold is None:
                return None, None
            self._refresh_frame()
            unfold = self._scan_button(
                skip | self._excluded_grid([fold]),
                want_visible=True,
                check_stop=check_stop,
                first=fold,
                ordered=self._right_edge_guard(
                    self._content_candidates(limit_rows=self.UNDO_SCAN_MAX_ROWS)
                ),
            )
            return fold, unfold
        unfold = self._scan_button(skip, want_visible=True, check_stop=check_stop)
        if unfold is None:
            return None, None
        self._refresh_frame()
        fold = self._scan_button(
            skip | self._excluded_grid([unfold]),
            want_visible=False,
            check_stop=check_stop,
            first=unfold,
            ordered=self._right_edge_guard(
                self._content_candidates(limit_rows=self.UNDO_SCAN_MAX_ROWS)
            ),
        )
        return fold, unfold

    def _try_brush_entry(
        self,
        skip: set[tuple[int, int]],
        retry_fold: Callable[[set[tuple[int, int]]], "tuple[float, float] | None"],
        check_stop: Callable[[], None],
    ) -> "tuple[tuple[float, float] | None, bool]":
        """画板可能停在初始态（底部只有 画笔/文字/贴纸）：点最左「画笔」进绘画面板。

        返回 (折叠钮或 None, 是否确认已进入绘画面板)。只在折叠钮探测失败后调用。
        安全约束（误点右侧「文字/贴纸」不可逆）：

          1. 只点画布下方区域最左 ≤3 列、底部 ≤3 行的候选——「画笔」永远是最
             左标签，第二三列即使脱靶也只会落在灰色空白，绝不越到右侧标签。
          2. 命中判据分两级：先用便宜的像素差分门控（进入绘画面板必然重构底部
             栏：撤销胶囊 + 工具行出现，空白处点击差分≈0）；差分显著后再做
             **结构确认**——宽段轨道可见或撤销胶囊带出现内容。结构确认与
             折叠钮重探解耦：真机浅灰轨道下重探必然失败，且其盲扫面会暴露给
             工具行最右的「✓ 确认」不可逆控件。
          3. 结构确认不通过（UI 与预期不符，可能误入文字/贴纸页）时立即放弃并
             提示人工检查，绝不继续盲点——继续点只会增加撞上不可逆控件的风险。
        """
        step = self._step
        by_row: dict[int, list[int]] = {}
        for x, y in self._candidates:
            if x > 2 * self.CANDIDATE_STEP:  # 最左 3 列（14/42/70），与右侧标签绝缘
                continue
            by_row.setdefault(y, []).append(x)
        # 从最贴截图底缘的行开始（初始态标签行就在窗口底部），最多底部
        # BRUSH_ENTRY_MAX_ROWS 行、每行最左 BRUSH_ENTRY_MAX_COLS 列。
        order = [
            (x, y)
            for y, row in sorted(by_row.items(), key=lambda kv: -kv[0])[
                : self.BRUSH_ENTRY_MAX_ROWS
            ]
            for x in sorted(row)[: self.BRUSH_ENTRY_MAX_COLS]
        ]
        if not order:
            return None, False
        step(STEP_BRUSH_ENTRY, STATE_RUNNING, None, "疑似初始态，尝试点最左画笔入口")

        before = self._grab()
        changed_min_px, changed_ratio = 150, 0.0005
        for point in order:
            if point in skip:
                continue
            check_stop()
            try:
                self._click(float(point[0]), float(point[1]))
            except ProbeError as exc:
                if exc.code == "E_PROBE_CLICK_IN_CANVAS":
                    continue
                raise
            self._sleep(self._settle_s)
            after = self._grab()
            if not self._below_canvas_changed(before, after, changed_min_px, changed_ratio):
                continue
            # 底部栏确实变了。进入绘画面板后候选网格按新帧重算，否则新工具行
            # 的候选会被当成「旧画布内」而整行丢失。
            self._candidates = self._make_candidates(after)
            self._undo_tried = set()
            entered = self._wide_track_visible(after) or bool(self._strip_candidates)
            if not entered:
                step(
                    STEP_BRUSH_ENTRY, STATE_FAILED, _point_geometry(point),
                    "底部已变化但未检出工具面板（无滑杆轨道/撤销胶囊）；"
                    "若误入文字/贴纸页请手动点回画笔再重试",
                )
                return None, False
            # 宽松档滑杆定带锚点：工具行是面板态最靠画布的内容行。撤销锚点
            # 缺失时（初始态无撤销胶囊可预扫）这是宽松档唯一的启用机会。
            if self._slider_anchor is None and self._row_has_content:
                self._slider_anchor = float(min(self._row_has_content))
            # 折叠钮重探只是尽力学坐标，不再承担入场判据。
            fold = retry_fold(skip)
            if fold is not None:
                step(STEP_BRUSH_ENTRY, STATE_DONE, _point_geometry(point),
                     "已进入绘画面板（折叠钮探测通过）")
                return fold, True
            # 自愈：若点在的是绘画面板已有的折叠钮（误把面板关掉），重探已把
            # 展开钮学到手，点开再继续判一次。
            if (self._learned_unfold is not None
                    and self._ensure_panel_open(None, check_stop)):
                fold = retry_fold(skip)
                if fold is not None:
                    step(STEP_BRUSH_ENTRY, STATE_DONE, _point_geometry(point),
                         "已在绘画面板（其间误触开合钮，已自动恢复）")
                    return fold, True
            step(STEP_BRUSH_ENTRY, STATE_DONE, _point_geometry(point),
                 "已进入绘画面板（结构判据：滑杆轨道/撤销胶囊出现），开合钮未学到")
            return None, True
        step(STEP_BRUSH_ENTRY, STATE_FAILED, None, "画布下方未探到有效的画笔入口")
        return None, False

    def _below_canvas_changed(
        self, before, after, min_px: int, min_ratio: float
    ) -> bool:
        """画布以下区域的像素差分：进入/离开绘画面板会重构整个底部栏。"""
        top = min(self._canvas[1] + self._canvas[3] + 8, after.shape[0])
        if top >= after.shape[0]:
            return False
        old = before[top:, :, 0] if before.ndim == 3 else before[top:, :]
        new = after[top:, :, 0] if after.ndim == 3 else after[top:, :]
        if old.shape != new.shape:
            return True
        changed = int(np.count_nonzero(np.abs(new.astype(np.int16) - old.astype(np.int16)) > 32))
        return changed >= min_px and changed / new.size >= min_ratio

    def _probe_fold_pair(
        self, skip: set[tuple[int, int]], check_stop: Callable[[], None]
    ) -> tuple[float, float] | None:
        """学习「折叠钮+展开钮」成对坐标；返回折叠钮，展开钮记入 _learned_unfold。

        命中判据是「点击→轨道可见性达到期望方向」+ 一次往返复验；未过复验
        的候选按干扰排除后重扫一轮，最多两轮。学不到就返回 None（滑杆步骤
        自行决定报错还是继续）。
        """
        blocked = set(skip)
        for _ in range(2):
            fold, unfold = self._learn_fold_pair_once(blocked, check_stop)
            if unfold is not None:
                self._learned_unfold = unfold
            if fold is None:
                break
            if unfold is not None and self._round_trip(fold, unfold, check_stop):
                # 往返复验以展开态收尾，恢复展开态候选网格，供后续滑杆/撤销/工具扫描。
                self._refresh_frame()
                return fold
            blocked |= self._excluded_grid([fold])
        # 一切失败路径：面板可能停在收起态，尽力恢复展开再交还主流程。
        self._ensure_panel_open(None, check_stop)
        return None

    def _ensure_panel_open(
        self, fold: tuple[float, float] | None, check_stop: Callable[[], None]
    ) -> bool:
        """展开面板：优先点已学到的展开钮，其次折叠钮/人工钮（可逆 UI 原位生效）。"""
        if self._panel_open():
            return True
        seen: set[tuple[float, float]] = set()
        for point in (self._learned_unfold, fold, self._manual_fold):
            if point is None:
                continue
            key = (float(point[0]), float(point[1]))
            if key in seen:
                continue
            seen.add(key)
            try:
                check_stop()
                self._click(key[0], key[1])
            except ProbeError as exc:
                if exc.code == "E_PROBE_CLICK_IN_CANVAS":
                    continue
                raise
            self._sleep(self._settle_s)
            if self._panel_open():
                return True
        return False

    def _transfer_rows(self, limit: int = 6) -> list[float]:
        """候选网格里距画布由近到远的前 limit 个行 y；工具行平移量远小于该覆盖。"""
        rows: list[float] = []
        for _x, y in self._candidates:
            if not rows or rows[-1] != y:
                rows.append(float(y))
                if len(rows) >= limit:
                    break
        return rows

    @staticmethod
    def _near_any(
        point: tuple[float, float], others: list[tuple[float, float] | None], radius: float
    ) -> bool:
        return any(
            other is not None
            and math.hypot(point[0] - float(other[0]), point[1] - float(other[1])) <= radius
            for other in others
        )

    def _excluded_grid(self, points: list["tuple[float, float] | None"]) -> set[tuple[int, int]]:
        """已知点位邻域的候选网格点集合（半径同 _skip_points），供盲扫跳过。"""
        radius = float(self.CANDIDATE_STEP)
        excluded: set[tuple[int, int]] = set()
        for item in points:
            if item is None:
                continue
            px, py = float(item[0]), float(item[1])
            for candidate in self._candidates:
                if math.hypot(candidate[0] - px, candidate[1] - py) <= radius:
                    excluded.add(candidate)
        return excluded

    def _try_click_outside_canvas(self, x: float, y: float) -> bool:
        """点击；落在当前（收起态）画布内则静默跳过该候选点。"""
        try:
            self._click(x, y)
            return True
        except ProbeError as exc:
            if exc.code == "E_PROBE_CLICK_IN_CANVAS":
                return False
            raise

    def _transfer_undo(
        self,
        undo: tuple[float, float],
        exclude: list[tuple[float, float] | None],
        check_stop: Callable[[], None],
    ) -> tuple[float, float] | None:
        """撤销按钮行平移：x 不变，逐候选行 y 试点击，判据与展开态完全一致。

        排除折叠钮等已知点位（误点会翻转面板把整个重扫带偏）；点击命中撤销后
        这笔探测线已被消耗，_probe_undos 记账保证清理预算不越界。
        """
        if not self._ensure_drawing_tool(self._excluded_grid(exclude), check_stop):
            return None
        baseline = self._band_occupied()
        if baseline <= self._band_eps():
            return None
        radius = float(self.CANDIDATE_STEP)
        for y in self._transfer_rows():
            point = (float(undo[0]), y)
            if self._near_any(point, exclude, radius):
                continue
            check_stop()
            if not self._try_click_outside_canvas(*point):
                continue
            self._sleep(self._settle_s)
            occupied = self._band_occupied()
            if occupied < baseline * self.UNDO_DROP_RATIO:
                self._probe_undos += 1
                return point
            if occupied < baseline * 0.9:
                if not self._ensure_drawing_tool(self._excluded_grid(exclude), check_stop):
                    return None
                baseline = max(baseline, self._band_occupied())
        return None

    def _transfer_tool(
        self,
        known: tuple[float, float] | None,
        rows: list[float],
        exclude: list[tuple[float, float] | None],
        check_stop: Callable[[], None],
        *,
        want_ink: bool,
    ) -> tuple[float, float] | None:
        """橡皮/画笔行平移：压线重描白化=橡皮、出新墨=画笔（同展开态判据）。"""
        if known is None:
            return None
        radius = float(self.CANDIDATE_STEP)
        for y in rows:
            point = (float(known[0]), y)
            if self._near_any(point, exclude, radius):
                continue
            check_stop()
            before = self._band_ink_pixels()
            if not self._try_click_outside_canvas(*point):
                continue
            self._sleep(self._settle_s)
            self._note_undo_like_drop(before)
            self._draw_band_line()
            if self._band_has_ink() == want_ink:
                return point
        return None

    def _fingerprint_closed(
        self,
        brush: tuple[float, float],
        eraser: tuple[float, float],
        check_stop: Callable[[], None],
    ) -> tuple[float | None, float | None]:
        """收起态画笔选中指纹：橡皮选中截画笔格=idle，切回画笔截=selected。"""
        check_stop()
        self._click(float(eraser[0]), float(eraser[1]))
        self._sleep(self._settle_s)
        idle_lum = self._cell_lum(brush)
        check_stop()
        self._click(float(brush[0]), float(brush[1]))
        self._sleep(self._settle_s)
        selected_lum = self._cell_lum(brush)
        if idle_lum is None or selected_lum is None:
            return selected_lum, idle_lum
        if abs(selected_lum - idle_lum) < self.LUM_DELTA_MIN:
            return None, None
        return selected_lum, idle_lum

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

    def _make_candidates(self, image) -> list[tuple[int, int]]:
        """画布下缘 +8 到截图底缘之间的网格点；跳过画布与刻度带内部。

        同一行内按「候选邻域的内容像素数」降序预排：工具按钮都有可见像素，
        大片留白处的候选几乎不可能是控件。只改行内顺序、不改变候选覆盖面
        （未选中态的工具格可能和背景几乎同色，剪枝会把它剪掉），让常见布局
        下命中大幅前置（盲扫从分钟级降到秒级）。

        内容像素用「邻域与该行背景中位灰度的差」而不是绝对暗度：真机工具条
        大量是浅灰控件（灰 240 按钮底、浅灰轨道），绝对阈值 160 会把它们全
        判成空白，而行内相对对比度天然免疫背景色。
        """
        height, width = image.shape[0], image.shape[1]
        self._image_width = width
        canvas = self._canvas
        y_start = canvas[1] + canvas[3] + 8
        if y_start >= height:
            self._row_ink = {}
            self._row_has_content = set()
            self._candidate_ink = {}
            self._candidate_centroid = {}
            self._strip_candidates = []
            self._strip_points = set()
            return []
        gray = image[..., 0] if image.ndim == 3 else image
        half = self.CANDIDATE_STEP // 2
        rows: dict[int, list[tuple[int, int, int]]] = {}
        centroids: dict[tuple[int, int], tuple[int, int]] = {}
        for y in range(y_start, height, self.CANDIDATE_STEP):
            band = gray[y : y + self.CANDIDATE_STEP, :]
            if band.size == 0:
                continue
            background = int(np.median(band))
            for x in range(self.CANDIDATE_STEP // 2, width, self.CANDIDATE_STEP):
                point = (int(x), int(y))
                if self._inside(canvas, point) or self._inside(self._band, point):
                    continue
                crop = gray[max(0, y - half) : y + half + 1, max(0, x - half) : x + half + 1]
                delta = np.abs(crop.astype(np.int16) - background)
                ink = int((delta > self.CONTENT_ROW_DELTA).sum())
                rows.setdefault(point[1], []).append((x, y, ink))
                # 内容质心：小按钮（如最左开合钮 ~19px）落在 28px 网格缝隙时，
                # 点网格点会点空；点邻域内内容质心才能落在按钮上。
                mask = delta > self.CONTENT_ROW_DELTA
                if mask.any():
                    x0 = max(0, x - half)
                    y0 = max(0, y - half)
                    ys, xs = np.nonzero(mask)
                    cx = int(round(float(xs.mean()) + x0))
                    cy = int(round(float(ys.mean()) + y0))
                    centroids[point] = (cx, cy)
        ordered: list[tuple[int, int]] = []
        row_ink: dict[int, int] = {}
        has_content: set[int] = set()
        candidate_ink: dict[tuple[int, int], int] = {}
        for y in sorted(rows):
            row = rows[y]
            row_ink[y] = sum(item[2] for item in row)
            if any(item[2] >= self.CONTENT_ROW_MIN_PX for item in row):
                has_content.add(y)
            for x, point_y, ink in row:
                candidate_ink[(x, point_y)] = ink
            order = sorted(range(len(row)), key=lambda i: (-row[i][2], i))
            ordered.extend((row[i][0], row[i][1]) for i in order)
        # 撤销胶囊带：画布底缘向上 CAPSULE_STRIP_ROWS 行、x 与预扫描安全区同规。
        # 胶囊可浮在画布内（展开态上移贴近画布中部），主候选网格只覆盖画布
        # 下方，够不着它；胶囊带点不参与任何盲扫，只供撤销探测。
        # 行级横向跨度门：画布左缘投影是 ~12px 宽、纵贯画布全高的暗列，若按
        # 单候选邻域 ink 计数会被逐行整列误登记（真机阴影列 ink 125~236）；
        # 撤销胶囊整行（撤销+重做两图标）横占 ~80 列，要求该行全宽内容列总数
        # 达 STRIP_ROW_MIN_COLS 才登记（登记点仍只取左半 x < strip_left，
        # 重做半区永不成为候选），阴影行被整体滤除。
        strip: list[tuple[int, int]] = []
        strip_left = int(float(canvas[0]) + self.CANDIDATE_STEP)
        for k in range(1, self.CAPSULE_STRIP_ROWS + 1):
            y = y_start - k * self.CANDIDATE_STEP
            if y < 0:
                break
            crop_top = max(0, y - half)
            band = gray[crop_top : y + half + 1, :]
            if band.size == 0:
                continue
            background = int(np.median(band))
            delta_row = np.abs(band.astype(np.int16) - background)
            content_cols = np.flatnonzero(
                (delta_row > self.CONTENT_ROW_DELTA).any(axis=0)
            )
            if content_cols.size < self.STRIP_ROW_MIN_COLS:
                continue
            for x in range(self.CANDIDATE_STEP // 2, strip_left, self.CANDIDATE_STEP):
                crop = gray[max(0, y - half) : y + half + 1, max(0, x - half) : x + half + 1]
                delta = np.abs(crop.astype(np.int16) - background)
                ink = int((delta > self.CONTENT_ROW_DELTA).sum())
                if ink >= self.CONTENT_ROW_MIN_PX:
                    point = (int(x), int(y))
                    strip.append(point)
                    candidate_ink[point] = ink
        self._strip_candidates = strip
        self._strip_points = set(strip)
        self._row_ink = row_ink
        self._row_has_content = has_content
        self._candidate_ink = candidate_ink
        self._candidate_centroid = centroids
        return ordered

    def _right_edge_guard(
        self, points: list[tuple[int, int]]
    ) -> list[tuple[int, int]]:
        """盲扫右缘防线：剔除右缘 reserve 列，并把每个内容行的最右内容格剔除。

        工具行最右恒为「✓ 确认」类不可逆控件（真机 ✓ 在 ~93% 宽处）。右缘
        reserve 与撤销扫描同规；但行内按内容量降序预排时，粗黑的 ✓ 会排在
        细灰开合钮之前被先点中，reserve 只挡坐标不挡顺序，因此再按结构先验
        剔掉每个内容行的最右格（x > 60% 才视为危险格，左单钮行不受影响；
        只剔单个候选点，画笔/橡皮擦格的其余候选仍可命中）。
        """
        reserve = max(
            self.SCAN_RIGHT_RESERVE_PX,
            int(self._image_width * self.SCAN_RIGHT_RESERVE_RATIO),
        )
        right_limit = self._image_width - reserve
        guarded = [p for p in points if p[0] <= right_limit]
        by_row: dict[int, list[tuple[int, int]]] = {}
        for point in guarded:
            if self._candidate_ink.get(point, 0) >= self.CONTENT_ROW_MIN_PX:
                by_row.setdefault(point[1], []).append(point)
        danger: set[tuple[int, int]] = set()
        danger_x = self._image_width * self.DANGER_CELL_MIN_X_RATIO
        for row_points in by_row.values():
            rightmost = max(row_points, key=lambda p: p[0])
            if rightmost[0] > danger_x:
                danger.add(rightmost)
        return [p for p in guarded if p not in danger and not self._mirror_excluded(p)]

    def _mirror_excluded(self, point: tuple[int, int]) -> bool:
        """撤销锚点建立后，同行右邻的「重做」半区候选一律剔除。

        撤销钮恒在胶囊左半、右半是重做：盲扫点中重做会把刚撤销的探测笔迹
        原样补回，撤销/清理预算全部失真（墨量跌了又涨回去，后续判据全乱）。
        只在撤销坐标已知时生效（同帧有效，换帧即在 _refresh_frame /
        _rescan_collapsed 清空），横向范围 UNDO_MIRROR_EXCLUDE_STEPS 个网格
        步长，覆盖胶囊右半图标而不至于吃掉更右侧的工具格。
        """
        anchor = self._undo_point
        if anchor is None:
            return False
        if abs(point[1] - anchor[1]) >= self.CANDIDATE_STEP // 2:
            return False
        return (
            point[0] > anchor[0]
            and point[0]
            <= anchor[0] + self.UNDO_MIRROR_EXCLUDE_STEPS * self.CANDIDATE_STEP
        )

    def _fold_scan_candidates(self) -> list[tuple[int, int]]:
        """折叠钮扫描序：有内容的候选行优先，行内按内容量降序。

        **候选覆盖面不变**（撤销锚点存在时是「撤销行及以下」，否则是预算内
        最近几行）——扩大扫描面会把底部「文字/贴纸」等不可逆标签纳入盲点
        范围，那些行只能由带差分门控的 brush_entry 流程处理。这里只调整顺序：
        工具条那几行必有图标/文字像素，先扫它们可避免鼠标先在空白行长时间
        爬行（看着像卡死）。

        撤销锚点成立时按结构先验「撤销在开合按钮上方」把扫描带下移到撤销行
        及其下方：真机窄高窗口里工具行距画布数百像素，画布下缘数 4 行的旧
        预算根本够不着，而撤销行以下的行几乎全是工具区，盲点风险反而更低。
        """
        if self._undo_row is not None:
            window = [
                point
                for point in self._candidates
                if point[1] >= self._undo_row - self.CANDIDATE_STEP // 2
            ]
        else:
            window = self._candidates[: self._fold_limit]
        if self._undo_tried:
            # 撤销（预）扫描已证伪的点不再重复盲点：它们刚给过「无墨量跌落」
            # 的负反馈，重复点击只烧时间。
            window = [p for p in window if p not in self._undo_tried]
        if not self._row_ink:
            return self._right_edge_guard(window)
        by_row: dict[int, list[tuple[int, int]]] = {}
        for point in window:
            by_row.setdefault(point[1], []).append(point)
        rows = sorted(by_row, key=lambda y: (-self._row_ink.get(y, 0), y))
        ordered: list[tuple[int, int]] = []
        for y in rows:
            ordered.extend(by_row[y])
        # 右缘守卫只缩面不改序：剔除「✓ 确认」所在的 reserve 列与各行最右格。
        return self._right_edge_guard(ordered)

    def _content_candidates(self, *, limit_rows: int) -> list[tuple[int, int]]:
        """内容行上的候选点（按行分组、行按内容量降序），上限 limit_rows 行。

        撤销盲扫走这条序：空白行一律不点（点了也没任何可观测反馈，纯烧时间），
        内容行预算 8 行足以跨过画布与工具行之间数百像素的空白带。右缘 reserve
        列（「√ 确认提交」不可逆区）直接剔除。
        """
        reserve = max(
            self.SCAN_RIGHT_RESERVE_PX,
            int(self._image_width * self.SCAN_RIGHT_RESERVE_RATIO),
        )
        right_limit = self._image_width - reserve
        return self._row_grouped(
            [
                p
                for p in self._candidates
                if p[0] <= right_limit and p[1] in self._row_has_content
            ],
            limit_rows,
        )

    def _prescan_candidates(self) -> list[tuple[int, int]]:
        """撤销预扫描安全区：绘画面板未确认前允许盲点的候选。

        预扫描发生在折叠钮/画笔入口之前——此刻还不知道画板处于绘画面板态
        还是初始态（底部是 画笔/文字/贴纸/提交创作 标签），而「文字/贴纸/
        提交创作」点了就跳页、不可自动回退。安全区只有一条硬约束：

          候选 x < 画布左缘 + 一个网格步长——撤销胶囊横跨画布左缘（左缘探出
          左缘之外、可点中心落在左缘之右，真机实测中心越界约 12px），只放行
          x < 左缘会点不中胶囊中心；放宽一个步长后仍远未触及「文字/贴纸」标签
          与「提交创作」大按钮（它们全在更右、更低的工具行，与撤销行不同带）。
          画布左缘之下只剩 ∨ 开合钮（可逆）与最左画笔标签（点了=进绘画面板，
          与 brush_entry 等效），两个可接受的误点面。

        候选还须是内容点（邻域内容像素达门槛），空白点一律不点。真机绘画面板
        收起态下撤销胶囊行内容量全图第一，命中通常在前几次点击内完成；找不到
        就返回空手，回退原折叠钮/入口流程。

        胶囊带（`_strip_candidates`）并入同一扫描面：展开态胶囊浮在画布**内**
        左下角，主候选网格（画布下方）覆盖不到，胶囊带是它唯一可达通道；
        胶囊带行 y 更小，按行升序自然排在最前。
        """
        by_row: dict[int, list[tuple[int, int]]] = {}
        # 撤销胶囊左缘探出画布左缘、但可点中心落在画布左缘之右（真机实测胶囊
        # 横跨左缘约 12px），只放行 x < 左缘会卡在胶囊最左的图标边缘、点不中。
        # 放宽到「左缘 + 一个网格步长」刚好覆盖胶囊中心，又不越过右侧
        # 「文字/贴纸」标签（那些在更右、更低的工具行，与撤销行不同带）。
        left_limit = float(self._canvas[0]) + self.CANDIDATE_STEP
        pooled = list(self._strip_candidates)
        for point in self._candidates:
            if point[0] >= left_limit:
                continue
            if self._candidate_ink.get(point, 0) < self.CONTENT_ROW_MIN_PX:
                continue
            pooled.append(point)
        for point in pooled:
            by_row.setdefault(point[1], []).append(point)
        rows = sorted(by_row)[: self.UNDO_SCAN_MAX_ROWS]
        ordered: list[tuple[int, int]] = []
        for y in rows:
            ordered.extend(by_row[y])
        return ordered

    def _row_grouped(
        self, pooled: list[tuple[int, int]], limit_rows: int
    ) -> list[tuple[int, int]]:
        """按「行内容量降序」重排候选，行按内容量取前 limit_rows 个。"""
        by_row: dict[int, list[tuple[int, int]]] = {}
        for point in pooled:
            by_row.setdefault(point[1], []).append(point)
        rows = sorted(by_row, key=lambda y: (-self._row_ink.get(y, 0), y))[:limit_rows]
        ordered: list[tuple[int, int]] = []
        for y in rows:
            ordered.extend(by_row[y])
        return ordered

    @staticmethod
    def _prioritize_row(
        points: list[tuple[int, int]], anchor_rows: list[float]
    ) -> list[tuple[int, int]]:
        """把落在任一锚点行 ±半格内的候选排到最前，其余保持原序。

        工具条控件同行是普遍布局；锚点来自已识别控件或人工坐标，行匹配容差
        取半格（14px），行内再按距锚点行的远近升序，保证最可能的行最先被扫。
        """
        half = ToolbarProber.CANDIDATE_STEP // 2
        in_row, rest = [], []
        for p in points:
            distance = min(abs(p[1] - row) for row in anchor_rows)
            if distance <= half:
                in_row.append((distance, len(in_row) + len(rest), p))
            else:
                rest.append(p)
        in_row.sort(key=lambda item: (item[0], item[1]))
        return [p for _, _, p in in_row] + rest

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
        """滑杆轨道检测（strict 暗段档）：也是面板「展开/收起」可见性判据。

        宽松档**不得**接在这里：收起帧里工具格（灰 180~240）相对白背景同样
        构成「相对暗段」，会让 ``_track_visible()`` 恒真、面板开合与收起态重扫
        全部失真。宽松档只由 :meth:`_locate_slider` 在显式找滑杆时兜底调用。
        """
        return self._find_slider_strict(image)

    def _locate_slider(self, image) -> dict | None:
        """显式找滑杆：strict 失败且有撤销锚点时按宽松档兜底。

        真机轨道浅灰（230~240），strict 档恒检不到；锚点成立 ⇔ 工具区确实在
        下方，此时宽松档误把短横杠/阴影带当轨道的代价只是一次笔宽调节失败，
        远小于整链报「未找到滑杆」丢掉全部探测成果。
        """
        strict = self._find_slider_strict(image)
        if strict is not None:
            return strict
        if self._slider_anchor is None:
            return None
        return self._find_slider_relaxed(image)

    def _find_slider_strict(self, image) -> dict | None:
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

    def _find_slider_relaxed(self, image) -> dict | None:
        """宽松档：以撤销行为顶带，按「行背景中位灰度 − 像素灰度」相对判据找轨道。

        真机轨道是浅灰（230~240），strict 档的绝对阈值 160 恒检不到；同时画布
        下缘投影带在绝对阈值放宽后会整行误报，所以宽松档必须①有撤销锚点才
        启用、②搜索带顶不越过撤销行上缘、③轨道长度限定在窗口宽度的
        [22%, 86%]——短于此的是撤销胶囊等按钮横杠，通宽的是阴影带/卡片边框。
        """
        gray = image[..., 0] if image.ndim == 3 else image
        height, width = gray.shape[0], gray.shape[1]
        canvas_bottom = self._canvas[1] + self._canvas[3] + 8
        margin = (
            self.CANDIDATE_STEP // 2
            + self.SLIDER_ANCHOR_MARGIN_ROWS * self.CANDIDATE_STEP
        )
        top = max(canvas_bottom, int(self._slider_anchor) - margin)
        if top >= height:
            return None
        region = gray[top:height, :]
        # 背景取整个搜索带的中位灰度（带内绝大多数像素是背景）。不能按行取
        # 中位：真机轨道可占行宽 60%~80%，行中位会被轨道本身拉偏成轨道灰度。
        background = int(np.median(region))
        min_run = max(self.SLIDER_MIN_RUN, int(width * self.SLIDER_RELAX_MIN_RUN_RATIO))
        max_run = int(width * self.SLIDER_RELAX_MAX_WIDTH_RATIO)

        masks = []
        current: list[int] = []
        groups: list[list[int]] = []
        for row in range(region.shape[0]):
            dark = (background - region[row].astype(np.int16)) > self.SLIDER_RELAX_DELTA
            run = self._longest_run(dark)
            masks.append(dark)
            if min_run < run < max_run:
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
            rows = np.vstack(masks[group[0] : group[-1] + 1])
            cols = np.flatnonzero(rows.any(axis=0))
            if cols.size == 0:
                continue
            x0, x1 = int(cols[0]), int(cols[-1]) + 1
            rect = {"x": x0, "y": int(top + group[0]), "w": x1 - x0, "h": len(group)}
            if not (min_run < rect["w"] < max_run):
                continue
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

    def _undo_scan_order(self) -> list[tuple[int, int]]:
        """撤销扫描默认序：胶囊带 → 左缘列内容点 → 其余内容行。

        结构先验「撤销胶囊恒在画布左缘附近、工具格恒在其右」：左缘列内容点
        必须先于按内容量降序的内容行——工具行内容量天然高于胶囊行，若按内容
        量排序会先点中画笔/橡皮擦格，而真机上收起态点工具格会展开面板、重构
        整个底部栏，旧帧候选网格随之失效（撤销胶囊上移进画布内，再也选不中）。
        """
        tried = self._undo_tried
        strip = [p for p in self._strip_candidates if p not in tried]
        left_limit = float(self._canvas[0]) + self.CANDIDATE_STEP
        left_col = [
            p
            for p in self._candidates
            if p[0] < left_limit
            and self._candidate_ink.get(p, 0) >= self.CONTENT_ROW_MIN_PX
            and p not in tried
        ]
        seen = set(strip) | set(left_col)
        rest = [
            p
            for p in self._content_candidates(limit_rows=self.UNDO_SCAN_MAX_ROWS)
            if p not in tried and p not in seen
        ]
        return strip + left_col + rest

    def _rowband_changed_px(self, before, after, y: float) -> int:
        """候选所在行带（±半格、全宽）的像素差分计数：撤销图标点亮/置灰反馈。"""
        height = min(before.shape[0], after.shape[0])
        half = self.CANDIDATE_STEP // 2
        top = max(0, int(y) - half)
        bottom = min(height, int(y) + half + 1)
        if top >= bottom:
            return 0
        a = before[top:bottom]
        b = after[top:bottom]
        if a.ndim == 3:
            a = a[..., 0]
        if b.ndim == 3:
            b = b[..., 0]
        return int((np.abs(b.astype(np.int16) - a.astype(np.int16)) > 32).sum())

    # ---------- 3) 撤销按钮 ----------

    def _probe_undo(
        self,
        fold: tuple[float, float] | None,
        slider: dict | None,
        eraser: tuple[float, float] | None,
        brush: tuple[float, float] | None,
        check_stop: Callable[[], None],
        *,
        ordered: list[tuple[int, int]] | None = None,
        allowed: list[tuple[int, int]] | None = None,
    ) -> tuple[float, float] | None:
        """先画基线，再逐候选点击；带内墨量降到基线 40% 以下者即撤销。

        撤销扫描是整条探测链的第一站：判据只依赖「画线→点击→墨量跌落」，
        不需要滑杆轨道、不需要面板展开态，因此即便折叠钮/轨道全部失效也能
        工作。默认扫描序见 `_undo_scan_order`（胶囊带 → 左缘列 → 内容行），
        命中后把所在行记为锚点，供折叠钮扫描定带、滑杆放宽档定顶带。
        扫描期 UI 重构感知见下方循环内注释。

        ``ordered`` 覆盖默认扫描序（预扫描传入画布左缘安全区）；``allowed``
        把内部 ``_ensure_drawing_tool`` 的补救点击同样收进该安全区，防止在
        绘画面板态确认之前误点初始态右侧「文字/贴纸」等不可逆标签。
        """
        skip = self._skip_points(
            fold=fold, slider=slider, eraser=eraser, brush=brush
        )
        if not self._ensure_drawing_tool(skip, check_stop, allowed=allowed):
            return None
        baseline = self._band_occupied()
        if baseline <= self._band_eps():
            return None
        default_order = ordered is None
        if ordered is None:
            ordered = self._undo_scan_order()
        total = len(ordered)
        ref = self._grab()
        rebuilds = 0
        scanned = 0
        i = 0
        while i < len(ordered):
            point = ordered[i]
            i += 1
            if point in skip:
                continue
            scanned += 1
            if scanned % 4 == 1:
                self._progress(
                    STEP_UNDO, f"正在识别撤销按钮（已试 {scanned}/{total} 个候选）"
                )
            check_stop()
            self._undo_tried.add(point)
            # 不用质心点击：真机实测撤销命中靠的是胶囊热区（整颗胶囊都可点），
            # 网格点足以命中；质心反而会被邻域其他控件拉偏，把「点哪个控件」
            # 的语义搞乱（工具格内容偏心时会被拽进相邻格）。
            before_frame = self._grab()
            try:
                self._click(*point)
            except ProbeError as exc:
                if exc.code == "E_PROBE_CLICK_IN_CANVAS":
                    continue
                raise
            self._sleep(self._settle_s)
            after = self._grab()
            occupied = ink_occupied_fraction(after, self._band)
            if self._debug_dir_now():
                print(
                    f"[DBG-UNDO] click={point} occ={occupied:.4f} base={baseline:.4f} "
                    f"react={self._rowband_changed_px(before_frame, after, point[1])} "
                    f"relayout={self._below_canvas_changed(ref, after, self.RELAYOUT_MIN_PX, self.RELAYOUT_MIN_RATIO)} "
                    f"default_order={default_order} canvas={self._canvas}",
                    flush=True,
                )
            if occupied < baseline * self.UNDO_DROP_RATIO:
                # 这一击已消耗掉一笔探测笔迹，清理时不再重复撤销。
                self._probe_undos += 1
                found = (float(point[0]), float(point[1]))
                self._undo_row = found[1]
                self._undo_point = (int(point[0]), int(point[1]))
                self._slider_anchor = found[1]
                return found
            # 重构感知：点击可能引发面板开合/页面跳转（典型：收起态点中画笔格
            # 展开面板，撤销胶囊上移进画布内左下角）——旧帧候选网格整体失效，
            # 按新帧重检画布、重算候选与胶囊带、清空已试集合后重建扫描序。
            # 仅默认扫描序重建：显式 ordered 是预扫描安全区，重建会让扫描面
            # 逸出安全区、碰到「文字/贴纸」等不可逆标签。必须排在反应复验
            # 之前：开合钮点击同样让行带重构，若先复验会把开合钮误认成撤销。
            if default_order and self._below_canvas_changed(
                ref, after, self.RELAYOUT_MIN_PX, self.RELAYOUT_MIN_RATIO
            ):
                rebuilds += 1
                if rebuilds > 2:
                    # 重构不止（UI 振荡）：旧帧坐标继续盲点无意义且有误触风险。
                    return None
                found_rect = detect_canvas_rect(after, max_ratio=self.CANVAS_MAX_RATIO)
                if found_rect is not None:
                    left, top, right, bottom = found_rect
                    self._canvas = (left, top, right - left, bottom - top)
                self._candidates = self._make_candidates(after)
                cols = max(1, math.ceil(after.shape[1] / self.CANDIDATE_STEP))
                self._fold_limit = min(
                    len(self._candidates), self.MAX_FOLD_ROWS * cols
                )
                self._undo_tried = set()
                # 新帧里胶囊已平移，旧撤销坐标对镜像半区剔除不再有效，
                # 待本轮重新学到后再生效。
                self._undo_point = None
                ordered = self._undo_scan_order()
                total = len(ordered)
                i = 0
                ref = after
                continue
            # 脏带逃逸 · 反应复验：带内叠有历史残留测试线时（历次探测失败
            # 留下的线与本次新线完全重合，画线位置固定），撤销生效后带内墨
            # 量仍纹丝不动，纯墨量判据结构性失明——真机实测「越失败越残留、
            # 越残留越失败」的死循环正源于此。兜底判据只对撤销结构先验区
            # （画布左缘 + 两个网格步长之左）启用：点击后该行带出现小幅像素
            # 重构（撤销图标置灰/重做图标点亮），复点一次同行带再次反应才
            # 认定——一次性高亮切换（画笔/橡皮选中态）不会二次反应，撤销钮
            # 每次点击都有反馈。两击各耗一笔 undo 历史，_probe_undos 记 2
            # 让清理预算相应收紧，不会越过探测起点。触发条件收得很紧：
            # 带内墨量与基线「纹丝不动」（≥98%）才进复验——墨量有任何回落
            # （橡皮类干扰的部分擦除）都交回下方补描分支按旧语义处理；复验
            # 只认「行带二次反应」，不认二次点击后的墨量跌落（对橡皮类无
            # 判别力）。触发前再挡掉底部大重构（预扫描不走上方重构检测，
            # 误点展开钮的行带重构近 2000px，不设限会被误认成撤销钮）。
            if (
                point[0] < float(self._canvas[0]) + 2 * self.CANDIDATE_STEP
                and occupied >= baseline * 0.98
                and not self._below_canvas_changed(
                    ref, after, self.RELAYOUT_MIN_PX, self.RELAYOUT_MIN_RATIO
                )
                and self._rowband_changed_px(before_frame, after, point[1])
                >= self.UNDO_REACTIVE_MIN_PX
            ):
                check_stop()
                self._click(*point)
                self._sleep(self._settle_s)
                after2 = self._grab()
                if self._debug_dir_now():
                    print(
                        f"[DBG-VERIFY] point={point} react2="
                        f"{self._rowband_changed_px(after, after2, point[1])} "
                        f"occ2={ink_occupied_fraction(after2, self._band):.4f}",
                        flush=True,
                    )
                # 只认「行带二次反应」，不认「二次点击后墨量跌落」：橡皮类
                # 干扰控件连点两次同样会把墨量砍下阈值，跌落判据对它无判别力；
                # 撤销/重做图标每次点击都明暗互换，行带反应才是撤销专属指纹。
                if self._rowband_changed_px(after, after2, point[1]) >= (
                    self.UNDO_REACTIVE_MIN_PX
                ):
                    self._probe_undos += 2
                    found = (float(point[0]), float(point[1]))
                    self._undo_row = found[1]
                    self._undo_point = (int(point[0]), int(point[1]))
                    self._slider_anchor = found[1]
                    return found
                before_frame = after2
                after = after2
                occupied = ink_occupied_fraction(after2, self._band)
            if occupied < baseline * 0.9:
                # 线被部分擦掉或意外消失：_ensure_drawing_tool 会切回绘制工具并
                # 重画一条线，不能再叠加一笔，否则撤销一次只掉一层、判据失真。
                if not self._ensure_drawing_tool(skip, check_stop, allowed=allowed):
                    return None
                baseline = max(baseline, self._band_occupied())
        return None

    def _ensure_drawing_tool(
        self,
        skip: set[tuple[int, int]],
        check_stop: Callable[[], None],
        *,
        allowed: list[tuple[int, int]] | None = None,
    ) -> bool:
        """当前若非画笔态（试画无墨），扫描候选直到切到能落墨的工具。

        ``allowed`` 非空时把扫描面限制在该集合内（撤销预扫描的画布左缘安全区），
        否则按内容行全量扫描。
        """
        if (
            self._probe_strokes > 0
            and self._band_occupied() > self._band_eps()
        ):
            # 带内还留着我们自己画过的测试线（撤销预扫描落过笔、而撤销一直
            # 未点中）：线本身就是画笔态证据。此时同位重描必然「无新增墨」，
            # 若据此误判工具不可用，既会重复叠笔（撤销一次撤不干净、判据
            # 失真），又会发起全网格盲点补救——有误触风险还白烧时间。
            return True
        if self._draw_band_line():
            return True
        scan = (
            self._content_candidates(limit_rows=self.UNDO_SCAN_MAX_ROWS)
            if allowed is None
            else allowed
        )
        for point in scan:
            if point in skip:
                continue
            check_stop()
            before = self._band_ink_pixels()
            try:
                self._click(*point)
            except ProbeError as exc:
                if exc.code == "E_PROBE_CLICK_IN_CANVAS":
                    continue
                raise
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

        命中控件只按**精确网格点**排除，不用半径邻域：相邻按钮中心距常小于
        一个网格步长（真机撤销/橡皮紧挨着），半径排除会把紧邻的另一个工具格
        一并剪掉；误重试点到已命中的格子也不会误报（判据方向相反）。
        """
        skip = self._skip_points(
            fold=fold, slider=slider, undo=undo, eraser=eraser, brush=brush
        )
        self._draw_band_line()
        if self._band_has_ink():
            self._tool_scan_label = "橡皮"
            found_eraser = self._scan_tool(skip, check_stop, want_ink=False)
            if found_eraser is not None:
                skip |= {tuple(int(v) for v in found_eraser)}
            self._tool_scan_label = "画笔"
            found_brush = self._scan_tool(skip, check_stop, want_ink=True)
        else:
            self._tool_scan_label = "画笔"
            found_brush = self._scan_tool(skip, check_stop, want_ink=True)
            if found_brush is not None:
                skip |= {tuple(int(v) for v in found_brush)}
            self._tool_scan_label = "橡皮"
            found_eraser = self._scan_tool(skip, check_stop, want_ink=False)
        return found_eraser, found_brush

    def _scan_tool(
        self,
        skip: set[tuple[int, int]],
        check_stop: Callable[[], None],
        *,
        want_ink: bool,
    ) -> tuple[float, float] | None:
        # 与折叠钮盲扫同规的右缘守卫：工具行最右的「✓ 确认」不可逆，不得盲点。
        ordered = self._right_edge_guard(self._candidates)
        for scanned, point in enumerate(ordered, start=1):
            if point in skip:
                continue
            if scanned % 4 == 1:
                self._progress(
                    STEP_BRUSH,
                    f"正在识别{self._tool_scan_label}按钮"
                    f"（已试 {scanned}/{len(ordered)} 个候选）",
                )
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

        落笔位置必须固定：橡皮判定依赖「重描后整条线消失」。撤销预算按「实际
        发出的笔数」计：真实应用的撤销历史对每笔（含重叠重描、橡皮空划）都留
        一条记录，只数「像素有变化」的笔会低估预算，撤不干净 → E_PROBE_UNCLEANED
        把整个探测成果丢掉。越撤风险由 _clear_probe_ink 的「回到基线即停」兜底。
        """
        bx = self._band[0] + 8
        by = self._band[1] + self._band[3] // 2
        before = self._band_ink_pixels()
        self._ensure_foreground()
        self._stroke((bx, by), (bx + self.BAND_LINE_LEN, by))
        self._sleep(0.12)
        self._probe_strokes += 1
        return self._band_ink_pixels() > before

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
        sx, sy = self._to_screen(float(undo[0]), float(undo[1]))
        print(
            f"[DBG-CLEAR] strokes={self._probe_strokes} undos={self._probe_undos} "
            f"remaining={remaining} baseline_px={self._band_baseline_px} "
            f"tolerance={tolerance} ink_before={self._band_ink_pixels()} "
            f"undo_img=({undo[0]:.1f},{undo[1]:.1f}) "
            f"undo_screen=({sx:.1f},{sy:.1f}) canvas={self._canvas} band={self._band}",
            flush=True,
        )
        for _ in range(remaining):
            check_stop()
            before_ink = self._band_ink_pixels()
            self._probe_undos += 1
            self._click(float(undo[0]), float(undo[1]))
            self._sleep(0.22)
            after_ink = self._band_ink_pixels()
            print(
                f"[DBG-CLEAR] undo#{_ + 1}/{remaining} screen=({sx:.1f},{sy:.1f}) "
                f"before={before_ink} after={after_ink}",
                flush=True,
            )
        if self._band_ink_pixels() > tolerance:
            print(
                f"[DBG-CLEAR] FAIL ink={self._band_ink_pixels()} > tolerance={tolerance}",
                flush=True,
            )
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
        # 唯一豁免是撤销胶囊带登记的内容点（展开态胶囊浮在画布内左下角，登记
        # 规则见 _make_candidates）——胶囊浮于画布之上，点击落在控件而非画布。
        if (
            self._inside(self._canvas, (float(x), float(y)))
            and (int(x), int(y)) not in self._strip_points
        ):
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
        self._snapshot(f"click_{int(x)}_{int(y)}")

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
        self._snapshot(f"stroke_{int(begin[0])}_{int(begin[1])}_{int(end[0])}_{int(end[1])}")

    def _grab(self):
        image = self._probe.capture_image()
        if image is None:
            raise ProbeError("E_PROBE_CAPTURE", "工具栏探测截图失败")
        return image
