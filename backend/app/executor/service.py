"""执行器服务：任务状态机、守护式倒计时、漂移校验、绘制线程与输入主权守护。

状态只由本服务单向推进（design 6.1），API 层只能发出停止意图。
进度以笔迹条数为单位；每完成一条笔迹持久化 cursor_path，约每 2% 推送一次事件。
绘制期间的守护信号见 executor/guard.py；stop_reason ∈ user/esc/pause/focus_lost/mouse_takeover/
canvas_touched/screen_changed（后两者为画布差分守护，见 docs/undo-rollback-design.md）。
暂停态的手动回退（undo_once）复用工具栏探测学到的撤销坐标。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image

from app.core.vision.canvas_detect import detect_canvas_rect
from app.device.capture import CaptureError
from app.device.geometry import Rect, image_point_to_screen
from app.device.service import DeviceService
from app.device.target import (
    DrawingTarget,
    Win32Target,
    find_live_hwnd_by_title,
    resolve_target,
)
from app.executor import canvas_blankness
from app.executor.guard import GuardProbe, Win32GuardProbe, evaluate_signals
from app.executor.ink_measure import detect_stray_ink, ink_pixels
from app.executor.native_mouse import NativeMouse, path_length
from app.executor.panel_control import PanelController
from app.executor.pen_calibration import CalibrationError, PenCalibrator
from app.executor.pen_order import width_buckets
from app.executor.toolbar_probe import (
    STATE_DONE,
    STATE_FAILED,
    STATE_MANUAL,
    STATE_RUNNING,
    STEP_BRUSH,
    STEP_CANVAS,
    STEP_ERASER,
    STEP_FOLD,
    STEP_LABELS,
    STEP_SLIDER,
    STEP_UNDO,
    ProbeError,
    ToolBitmap,
    ToolbarProber,
)
from app.store.models import CanvasProfile, PlanRecord, TaskRecord, utc_now
from app.store.store import Store

TERMINAL_STATUSES = ("DONE", "FAILED", "STOPPED")

# 一键自动校准终态后事件总线的保留窗口：给慢 SSE 客户端补尾，超期惰性清理。
_CAL_RUN_RETAIN_S = 120.0

# 倒计时最后这段时间要求目标在前台，不满足则冻结倒计时等待用户切回。
ARM_FINAL_WINDOW_S = 1.0
ARM_STABLE_S = 0.5

# 标定扫描档数：只用于判断滑杆是否真的能调宽（宽度 spread），
# 不再对应绘制档数/桶数（桶数由 width_tolerance 推导）。
CALIBRATION_PROBE_TIERS = 5

# 探测刻度带工作区：画布顶部条带，只用于工具栏探测自己的落笔与观测。
# 标定基线已改由 PenCalibrator 用其真实 band 计算，不再复用这里的推导。
BAND_ROW_STEP_PX = 14  # 与 PenCalibrator 的最小行距一致

# 工具条行为探测的空白前提：探测会在画布下方候选区逐点盲点，若画布已有墨迹
# （用户画作或上次未清理的笔迹），可能点中「清空/颜色」等无法恢复的控件。
# 判定统一走 executor/canvas_blankness（自适应背景 + 空白基线 + 绝对封顶）。

# 回退生效判定（undo-rollback-design §3.3）：一笔误触墨迹至少几百像素，渲染
# 抖动在个位数；判据宁低勿高——误报 effective=false 只让用户再点一次，漏报
# 会误导用户少撤。画布墨量低于空白阈值时不发点击（空历史撤无可撤无效果）。
UNDO_EFFECT_MIN_PIXELS = 8
UNDO_EFFECT_MIN_FRACTION = 0.001
UNDO_SETTLE_S = 0.45  # 与工具条探测同一沉降节奏
CLEAR_UNDO_MAX_CLICKS = 80  # 清空画布的撤销点击预算（clear-and-guide-design §4.3）
CLEAR_PLATEAU_CLICKS = 3  # 连续 N 次墨量不动判「撤到底」，容忍撤销动画中途态
CLEAR_SETTLE_S = 0.22  # 清空循环内每次撤销后的沉降（与 PenCalibrator 清画布同节奏）
# 橡皮兜底（§4.4）：无标定表时的最粗笔宽估计，用于推行距（行距 =
# max(最粗档, 该值) × 0.5，保证垂直方向 50% 重叠）。
ERASER_FALLBACK_WIDTH_PX = 28.0
ERASER_MAX_ROUNDS = 2
ERASER_ROW_EDGE_INSET_PX = 4  # 首末行压住画布上下缘内 4px
ERASER_ROW_OVERSCAN_PX = 8  # 左右各超出画布 8px（画布外无害）

# 画布差分守护（undo-rollback-design §4.2）：允许区域半径覆盖漂移容差与
# 端点圆帽；像素下限过滤渲染抖动/动画元素的零星变化。
STRAY_RADIUS_PX = 16
STRAY_MIN_PIXELS = 40

# 守护线程对信号读取瞬时故障的容忍轮数（默认轮询 50ms ≈ 1 秒）：连续失败超过
# 该值才放弃守护并发 warning；偶发抖动绝不允许静默终结守护（否则 ESC/接管全失灵）。
GUARD_ERROR_LIMIT = 20


class TaskError(Exception):
    """携带 HTTP 状态与业务错误码的执行器失败。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ExecParams:
    event_step_px: float = 1.0
    point_delay_ms: float = 1.5
    stroke_delay_ms: float = 6.0
    click_hold_ms: float = 20.0
    no_coalesce: bool = True
    short_stroke_protect: bool = True
    short_stroke_threshold_px: float = 1.2
    focus_guard: bool = True
    cursor_takeover_px: float = 6.0
    guard_poll_ms: float = 50.0
    # 画布差分守护：mirror 目标上每条笔迹结束后比对画布，检出手指误触墨迹。
    canvas_guard: bool = True
    # 自动变粗：绘制前探测工具栏、标定笔宽，并按目标行程逐段分配笔宽。
    auto_width: bool = True
    # 换宽容差：目标行程差超过该值才换一次笔（0.02~0.20，默认 0.05 ≈ 最多 20 档）。
    width_tolerance: float = 0.05
    # 标定产出并回写 exec_params 的宽度表 [(滑杆位置0..1, 实测宽度px)]；续画复用。
    width_table: tuple[tuple[float, float], ...] | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "ExecParams":
        return cls(
            event_step_px=float(data.get("eventStepPx", 1.0)),
            point_delay_ms=float(data.get("pointDelayMs", 1.5)),
            stroke_delay_ms=float(data.get("strokeDelayMs", 6.0)),
            click_hold_ms=float(data.get("clickHoldMs", 20.0)),
            no_coalesce=bool(data.get("noCoalesce", True)),
            short_stroke_protect=bool(data.get("shortStrokeProtect", True)),
            short_stroke_threshold_px=float(data.get("shortStrokeThresholdPx", 1.2)),
            focus_guard=bool(data.get("focusGuard", True)),
            cursor_takeover_px=float(data.get("cursorTakeoverPx", 6.0)),
            guard_poll_ms=float(data.get("guardPollMs", 50.0)),
            canvas_guard=bool(data.get("canvasGuard", True)),
            auto_width=bool(data.get("autoWidth", True)),
            # 旧字段 widthTiers 已被容差语义取代，刻意忽略以兼容历史请求体。
            width_tolerance=float(data.get("widthTolerance", 0.05)),
            width_table=(
                tuple((float(a), float(b)) for a, b in data["widthTable"])
                if data.get("widthTable")
                else None
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "eventStepPx": self.event_step_px,
            "pointDelayMs": self.point_delay_ms,
            "strokeDelayMs": self.stroke_delay_ms,
            "clickHoldMs": self.click_hold_ms,
            "noCoalesce": self.no_coalesce,
            "shortStrokeProtect": self.short_stroke_protect,
            "shortStrokeThresholdPx": self.short_stroke_threshold_px,
            "focusGuard": self.focus_guard,
            "cursorTakeoverPx": self.cursor_takeover_px,
            "guardPollMs": self.guard_poll_ms,
            "canvasGuard": self.canvas_guard,
            "autoWidth": self.auto_width,
            "widthTolerance": self.width_tolerance,
            "widthTable": [list(t) for t in self.width_table] if self.width_table else None,
        }


@dataclass
class _RunHandle:
    """单次执行的停止信号；生命周期与线程严格绑定，避免跨任务串扰。"""

    stop_event: threading.Event = field(default_factory=threading.Event)
    guard_event: threading.Event = field(default_factory=threading.Event)
    # 守护线程仅在 DRAWING 阶段评估焦点/主权信号（ESC 不受此限制）。
    guard_active: threading.Event = field(default_factory=threading.Event)
    # 笔画进行中不比较光标位置——中间点本身在高速移动。
    stroking: threading.Event = field(default_factory=threading.Event)
    reason: str | None = None
    # 两条笔迹之间注入器精确已知的光标位置（屏幕坐标）。
    settled_cursor: tuple[int, int] | None = None

    def request_stop(self, reason: str) -> None:
        if self.reason is None:
            self.reason = reason
        self.stop_event.set()


@dataclass
class _CalibrationRun:
    """一键自动校准的内存事件总线：SSE 端点轮询 events，结束后延迟清理。"""

    handle: "_RunHandle"
    events: list[dict] = field(default_factory=list)
    done: bool = False
    finished_at: float = 0.0


def step_to_warning(event: dict, prefix: str = "") -> dict:
    """探测步骤事件 → 校准 SSE 的 calibration.warning payload。"""
    label = STEP_LABELS.get(event.get("step", ""), event.get("step", ""))
    state = event.get("state")
    if state == STATE_DONE:
        message = f"{prefix}已识别 {label}"
    elif state == STATE_FAILED:
        detail = event.get("evidence") or ""
        message = f"{prefix}未识别 {label}" + (f"：{detail}" if detail else "")
    elif event.get("evidence"):
        message = f"{prefix}{label}：{event['evidence']}"
    else:
        message = f"{prefix}{label}：探测中"
    return {"message": message, "step": event.get("step"), "state": state}


def _rect_geometry_of(rect: Rect) -> dict:
    """Rect → 校准 SSE 的几何事件载荷（截图坐标系）。"""
    return {"kind": "rect", **rect.as_dict()}


def _rect_iou(a: Rect, b: Rect) -> float:
    ax2, ay2 = a.x + a.w, a.y + a.h
    bx2, by2 = b.x + b.w, b.y + b.h
    ix = max(0, min(ax2, bx2) - max(a.x, b.x))
    iy = max(0, min(ay2, by2) - max(a.y, b.y))
    inter = ix * iy
    union = a.w * a.h + b.w * b.h - inter
    return inter / union if union > 0 else 0.0


def _point_in_rect(point: tuple[float, float], rect: Rect) -> bool:
    x, y = point
    return rect.x <= x <= rect.x + rect.w and rect.y <= y <= rect.y + rect.h


def _is_panel_state_change(saved: Rect, current: Rect) -> bool:
    """画布仅高度变化（左/上/宽不动）＝ 工具面板开合，而非窗口移动。

    画板底部工具面板展开时画布白区下缘上移、收起时下移，x/top/width 恒定。
    漂移门禁的本意是拦「窗口被移动/缩放导致控件坐标失效」，若把面板开合也
    判成漂移，会出现「刚校准完就报 E_CANVAS_DRIFT」的死结（校准存收起态、
    开画要展开面板），且此时映射基准本就按当前帧重取，坐标仍然有效。
    """
    if abs(saved.x - current.x) > 3 or abs(saved.y - current.y) > 3:
        return False
    if abs(saved.w - current.w) > max(3, int(saved.w * 0.01)):
        return False
    return saved.h != current.h


class ExecutorService:
    """全局至多一个活动绘制任务；执行在独立线程，不共享可变对象。"""

    def __init__(
        self,
        store: Store,
        device: DeviceService,
        mouse_factory: Callable[[], object] = NativeMouse,
        guard_probe: GuardProbe | None = None,
        arm_final_window_s: float = ARM_FINAL_WINDOW_S,
        arm_stable_s: float = ARM_STABLE_S,
    ) -> None:
        self._store = store
        self._device = device
        self._mouse_factory = mouse_factory
        self._probe = guard_probe or Win32GuardProbe()
        self._arm_final_window_s = arm_final_window_s
        self._arm_stable_s = arm_stable_s
        self._lock = threading.Lock()
        self._handle: _RunHandle | None = None
        # 一键自动校准的内存事件总线（project_id → run）；SSE 端点轮询读取。
        self._cal_runs: dict[str, _CalibrationRun] = {}

    # ---------- 对外入口（API 线程调用） ----------

    def start(
        self,
        *,
        plan: PlanRecord,
        exec_params: dict,
        countdown_ms: int,
        allow_drift: float,
    ) -> tuple[TaskRecord, int]:
        params = ExecParams.from_dict(exec_params)
        paths = self._load_paths(plan)
        target = self._target(plan)
        handle, mouse = self._claim(target)

        try:
            record = self._store.create_task(plan_id=plan.id, exec_params=params.as_dict())
            self._arm(record.id)
            record = self._store.get_task(record.id) or record
        except BaseException:
            self._release(handle)
            raise

        self._spawn(record, plan, paths, params, mouse, target, countdown_ms, allow_drift, handle=handle)
        return record, len(paths)

    def resume(
        self,
        *,
        task_id: str,
        countdown_ms: int,
        allow_drift: float,
    ) -> tuple[TaskRecord, int]:
        record = self._store.get_task(task_id)
        if record is None:
            raise TaskError(404, "E_NOT_FOUND", "任务不存在")
        if record.status != "STOPPED":
            raise TaskError(409, "E_TASK_CONFLICT", "仅停止状态的任务可以续画")
        plan = self._store.get_plan(record.plan_id)
        if plan is None:
            raise TaskError(404, "E_NOT_FOUND", "规划不存在")
        paths = self._load_paths(plan)
        target = self._target(plan)
        handle, mouse = self._claim(target)

        try:
            self._store.reopen_task(task_id)
            self._arm(task_id)
        except BaseException:
            self._release(handle)
            raise

        record = self._store.get_task(task_id)
        assert record is not None
        params = ExecParams.from_dict(record.exec_params)
        cursor = record.cursor_path
        if cursor > 0 and self._legacy_tier_order(record.exec_params):
            # 升级前（widthTiers 档位序）创建的断点：笔序与当前容差分桶不同，
            # 从旧游标续画会漏画/错宽；本次从第 1 笔重画，避免静默错序。
            # 同时把 exec_params 迁移成当前格式（as_dict 不再输出 widthTiers），
            # 否则下次续画会重复触发这段重置与 warning。
            self._emit(task_id, "task.warning", {
                "message": "该任务由旧版按档位顺序创建，断点笔序与当前算法不同；"
                           "已从第 1 笔重新开始（请确认画布已清空）"
            })
            self._store.update_task(
                task_id, cursor_path=0, exec_params=params.as_dict()
            )
            cursor = 0
            record = self._store.get_task(task_id) or record
        self._spawn(
            record,
            plan,
            paths,
            params,
            mouse,
            target,
            countdown_ms,
            allow_drift,
            cursor=cursor,
            handle=handle,
        )
        return record, len(paths)

    def undo_once(self, task_id: str) -> dict:
        """STOPPED 态回退一笔：点目标画布自己的撤销按钮一次并验证墨量下降。

        同步执行（约 1-2 s），短暂占用全局执行槽位，与 start/resume 天然互斥。
        不改任务状态、游标或参数——回退只是画布上的清理动作，续画仍走 /resume
        （docs/undo-rollback-design.md §3）。
        """
        record = self._store.get_task(task_id)
        if record is None:
            raise TaskError(404, "E_NOT_FOUND", "任务不存在")
        if record.status != "STOPPED":
            raise TaskError(409, "E_TASK_CONFLICT", "仅停止状态的任务可以回退笔画")
        plan = self._store.get_plan(record.plan_id)
        if plan is None:
            raise TaskError(404, "E_NOT_FOUND", "规划不存在")
        calibration = self._store.latest_calibration(plan.project_id)
        if calibration is None:
            raise TaskError(422, "E_CANVAS_MISSING", "项目尚未完成画布校准")
        undo_saved = self._point_dict(calibration.undo)
        tools_saved = (calibration.tools_open or {}).get("undo")
        if undo_saved is None and tools_saved is None:
            raise TaskError(
                422, "E_UNDO_UNAVAILABLE",
                "校准中没有撤销按钮坐标：请到校准页补标撤销按钮（自动探测也会在学习后补上）",
            )

        target = self._target(plan)
        handle, mouse = self._claim(target)
        try:
            return self._undo_with_handle(task_id, calibration, target, mouse, handle)
        except TaskError:
            raise
        except Exception as exc:  # 点击/截图等未分类异常统一转回退失败
            raise TaskError(409, "E_UNDO_FAILED", f"回退执行失败：{exc}") from exc
        finally:
            try:
                mouse.up()  # type: ignore[attr-defined]
            except Exception:
                pass
            self._release(handle)

    def _undo_with_handle(
        self,
        task_id: str,
        calibration: CanvasProfile,
        target: DrawingTarget,
        mouse: object,
        handle: _RunHandle,
    ) -> dict:
        if not target.foreground_matches():
            target.activate()
            time.sleep(0.15)

        image = target.capture_image()
        if image is None or not target.alive():
            raise TaskError(409, "E_WINDOW_LOST", target.lost_hint)
        found = detect_canvas_rect(image)
        if found is None:
            raise TaskError(409, "E_UNDO_FAILED", "当前画面未识别到画布，无法回退")
        l, t, r, b = found
        canvas = Rect(l, t, r - l, b - t)
        saved_rect = Rect(calibration.rect_x, calibration.rect_y, calibration.rect_w, calibration.rect_h)
        # 撤销坐标来自校准帧；窗口移动过就要按当前画布矩形整体平移，否则点到别处。
        dx, dy = canvas.x - saved_rect.x, canvas.y - saved_rect.y

        undo_open = self._point_dict((calibration.tools_open or {}).get("undo"))
        # tools_open 帧是探测时的 new_rect 帧；latest_calibration 即最新记录，
        # 两态坐标都按「校准 rect → 当前 rect」平移，与 _run 的 dx,dy 语义一致。
        candidates = [
            p for p in (
                self._shift_point(undo_open, dx, dy),
                self._shift_point(self._point_dict(calibration.undo), dx, dy),
            )
            if p is not None
        ]
        if not candidates:
            raise TaskError(
                422, "E_UNDO_UNAVAILABLE", "校准中没有可用的撤销按钮坐标，请到校准页补标",
            )

        image_h, image_w = image.shape[:2]
        client = target.client_area()
        region = (canvas.x, canvas.y, canvas.w, canvas.h)

        before = ink_pixels(image, region)
        if self._is_canvas_blank(image, region, calibration):
            # 空画布对着空历史点撤销没有可观测效果，也不值得再开合一次面板。
            return {
                "taskId": task_id, "effective": False,
                "inkBefore": before, "inkAfter": before,
                "message": "画布已无墨迹，无需回退",
            }

        slider_saved = self._shift_rect(calibration.slider, dx, dy)
        fold_open = self._point_dict((calibration.tools_open or {}).get("fold")) \
            or self._point_dict(calibration.fold)
        fold_open = self._shift_point(fold_open, dx, dy) if fold_open else None
        fold_closed = self._shift_point(
            self._point_dict((calibration.tools_collapsed or {}).get("fold")), dx, dy
        )
        panel = None
        if slider_saved is not None and fold_open is not None:
            panel = PanelController(
                probe=target,
                mouse=mouse,
                to_screen=lambda x, y: image_point_to_screen(x, y, image_w, image_h, client),
                stopped=handle.stop_event.is_set,
                sleep=handle.stop_event.wait,
            )
            if panel.is_open(slider_saved) is not True:
                if not panel.unfold(
                    slider_saved, self._button_chain(fold_closed, fold_open)
                ):
                    raise TaskError(
                        409, "E_UNDO_FAILED",
                        "工具面板无法展开（撤销按钮只在面板展开时可见），已停止回退",
                    )
            # 展开后画布可能被工具条撑大：跨态点击必须用「点击那一刻」的画布做守卫。
            guard_canvas = self._current_canvas_rect(target) or canvas
        else:
            guard_canvas = canvas

        point = next(
            (p for p in candidates if not _point_in_rect((p["x"], p["y"]), guard_canvas)),
            None,
        )
        if point is None:
            if panel is not None:
                panel.fold(slider_saved, self._button_chain(fold_open, fold_closed))
            raise TaskError(
                409, "E_UNDO_FAILED",
                "撤销按钮坐标落在画布内（画布可能已漂移），已停止回退；请重新校准",
            )

        try:
            if not self._click_image_point(
                mouse, target, (point["x"], point["y"]), (image_w, image_h), client,
                canvas=guard_canvas,
            ):
                raise TaskError(409, "E_UNDO_FAILED", "撤销点击被画布守卫拒绝")
            if handle.stop_event.wait(UNDO_SETTLE_S):
                raise TaskError(409, "E_TASK_CONFLICT", "回退过程中收到停止请求")
            after_image = target.capture_image()
        finally:
            # 与 _ensure_brush_before_draw 同一收尾约定：展开过就尝试折回，
            # PanelController 自身的画布守卫会拒掉「按钮被撑大的画布盖住」时的盲点。
            if panel is not None:
                panel.fold(slider_saved, self._button_chain(fold_open, fold_closed))

        if after_image is None:
            raise TaskError(409, "E_UNDO_FAILED", "回退后的复检截图失败")
        after = ink_pixels(after_image, region)
        dropped = before - after
        effective = dropped >= UNDO_EFFECT_MIN_PIXELS and (
            dropped >= UNDO_EFFECT_MIN_FRACTION * max(1.0, before)
        )
        message = (
            f"撤销已生效：画布墨迹 −{dropped} px，请对照投屏画面确认"
            if effective
            else "撤销无效果：可能已撤到底，请目视确认后决定续画或清空重画"
        )
        return {
            "taskId": task_id, "effective": effective,
            "inkBefore": before, "inkAfter": after, "message": message,
        }

    # ---------- 清空画布（clear-and-guide-design §4） ----------

    def clear_canvas(self, project_id: str, *, confirm_dirty_probe: bool = False) -> dict:
        """项目级清空画布：连点目标自己的撤销按钮直到画布空白。

        同步执行（预算内 ≤ 60 s），短暂占用全局执行槽位，与 start/resume/undo
        互斥。用户点「清空」即授权画布上的墨全部可弃——这是与回退一笔
        （逐笔确认）不同的授权边界。橡皮兜底与脏画布探测在 C1/C2 接入。
        """
        if self._store.get_project(project_id) is None:
            raise TaskError(404, "E_NOT_FOUND", "项目不存在")
        calibration = self._store.latest_calibration(project_id)
        if calibration is None or calibration.rect_w <= 0 or calibration.rect_h <= 0:
            raise TaskError(422, "E_CANVAS_MISSING", "项目尚未完成画布校准")

        target = self._target_for_project(project_id)
        handle, mouse = self._claim(target)
        try:
            # 连点撤销最长可达 60 s，ESC/空格随时急停。
            self._start_esc_watch(mouse, target, handle)
            return self._clear_with_handle(
                project_id, calibration, target, mouse, handle,
                confirm_dirty_probe=confirm_dirty_probe,
            )
        except TaskError:
            raise
        except Exception as exc:  # 点击/截图等未分类异常统一转清空失败
            raise TaskError(409, "E_CLEAR_FAILED", f"清空执行失败：{exc}") from exc
        finally:
            handle.guard_event.set()
            try:
                mouse.up()  # type: ignore[attr-defined]
            except Exception:
                pass
            self._release(handle)

    def _clear_with_handle(
        self,
        project_id: str,
        calibration: CanvasProfile,
        target: DrawingTarget,
        mouse: object,
        handle: _RunHandle,
        *,
        confirm_dirty_probe: bool = False,
    ) -> dict:
        if not target.foreground_matches():
            target.activate()
            time.sleep(0.15)

        image = target.capture_image()
        if image is None or not target.alive():
            raise TaskError(409, "E_WINDOW_LOST", target.lost_hint)
        found = detect_canvas_rect(image)
        if found is None:
            raise TaskError(409, "E_CLEAR_FAILED", "当前画面未识别到画布，无法清空")
        l, t, r, b = found
        canvas = Rect(l, t, r - l, b - t)
        saved_rect = Rect(
            calibration.rect_x, calibration.rect_y, calibration.rect_w, calibration.rect_h
        )
        # 校准坐标来自旧帧；窗口移动/缩放后一律按「校准 rect → 当前 rect」平移。
        dx, dy = canvas.x - saved_rect.x, canvas.y - saved_rect.y
        image_h, image_w = image.shape[:2]
        client = target.client_area()
        region = (canvas.x, canvas.y, canvas.w, canvas.h)

        ink_before = ink_pixels(image, region)
        if self._is_canvas_blank(image, region, calibration):
            return {
                "cleared": True, "method": "already_blank",
                "inkBefore": ink_before, "inkAfter": ink_before,
                "message": "画布已无墨迹，无需清空",
            }

        undo_point = self._clear_undo_point(calibration, dx, dy)
        if undo_point is None:
            if not confirm_dirty_probe:
                raise TaskError(
                    422, "E_CLEAR_UNAVAILABLE",
                    "还没学到撤销按钮的位置：确认画布上的墨迹可以丢弃后重试，"
                    "程序将自动学习工具条控件位置",
                )
            # 脏画布探测（§6.2）：用户已授权丢弃墨迹，允许在有墨状态下点击
            # 画布下方工具条区域学习控件坐标；成功后重新读校准取撤销坐标。
            slider_probe = self._shift_rect(calibration.slider, dx, dy)
            fold_probe = self._shift_point(self._point_dict(calibration.fold), dx, dy)
            self._probe_toolbar(
                None, project_id, calibration, target, mouse, client,
                (image_w, image_h), canvas, dx, dy,
                fold_probe, slider_probe,
                self._shift_point(self._point_dict(calibration.undo), dx, dy),
                handle,
            )
            calibration = self._calibration_now(project_id, calibration)
            undo_point = self._clear_undo_point(calibration, dx, dy)
            if undo_point is None:
                raise TaskError(
                    422, "E_CLEAR_UNAVAILABLE",
                    "未能自动学到工具条撤销按钮位置，请到校准页补标撤销按钮后重试",
                )

        slider_saved = self._shift_rect(calibration.slider, dx, dy)
        fold_saved = self._point_dict(calibration.fold) or self._point_dict(
            (calibration.tools_open or {}).get("fold")
        )
        fold_point = self._shift_point(fold_saved, dx, dy) if fold_saved else None
        fold_closed_clear = self._shift_point(
            self._point_dict((calibration.tools_collapsed or {}).get("fold")), dx, dy
        )
        panel = None
        if slider_saved is not None and fold_point is not None:
            panel = PanelController(
                probe=target,
                mouse=mouse,
                to_screen=lambda x, y: image_point_to_screen(
                    x, y, image_w, image_h, client
                ),
                stopped=handle.stop_event.is_set,
                sleep=handle.stop_event.wait,
            )
            if panel.is_open(slider_saved) is not True:
                if not panel.unfold(
                    slider_saved, self._button_chain(fold_closed_clear, fold_point)
                ):
                    raise TaskError(
                        409, "E_CLEAR_FAILED",
                        "工具面板无法展开（撤销按钮只在面板展开时可见），已停止清空",
                    )
            # 展开后画布可能被工具条撑大：跨态点击必须用「点击那一刻」的画布做守卫。
            guard_canvas = self._current_canvas_rect(target) or canvas
        else:
            guard_canvas = canvas

        try:
            cleared, clicks, ink_after, final_image = self._undo_until_blank(
                calibration, image, region, undo_point, guard_canvas,
                image_size=(image_w, image_h), client=client, mouse=mouse,
                target=target, handle=handle,
            )

            method = "undo"
            switch_warning = ""
            if not cleared and not handle.stop_event.is_set():
                # 撤到底仍带墨 → 橡皮兜底（§4.4）。缺橡皮/滑杆坐标时跳过兜底，
                # 由下方复查统一报「撤销深度不足」。
                tools = self._tool_bitmap(
                    getattr(calibration, "tools_open", None), dx, dy
                )
                eraser_point = (
                    (tools.eraser, tools.brush, tools)
                    if tools is not None and tools.eraser is not None
                    and tools.brush is not None and slider_saved is not None
                    else None
                )
                if eraser_point is not None and final_image is not None:
                    point, brush_point, bit = eraser_point
                    if self._eraser_sweep(
                        point, brush_point, bit, slider_saved, region,
                        final_image, guard_canvas,
                        image_size=(image_w, image_h), client=client, mouse=mouse,
                        target=target, handle=handle, calibration=calibration,
                        widest_px=self._widest_width_px(project_id),
                    ):
                        cleared = True
                        method = "undo+eraser"
                    # 兜底流程切过橡皮；无论成败都必须切回画笔（§4.4 步 6），
                    # 否则用户接手时手里是橡皮，一画就把残留擦没了。
                    switch_warning = self._switch_back_to_brush(
                        brush_point, bit, guard_canvas,
                        image_size=(image_w, image_h), client=client,
                        mouse=mouse, target=target,
                    )
                    after_brush = target.capture_image()
                    if after_brush is not None:
                        final_image = after_brush
        finally:
            if panel is not None:
                panel.fold(slider_saved, self._button_chain(fold_point, fold_closed_clear))

        if handle.stop_event.is_set():
            return {
                "cleared": cleared, "method": "undo",
                "inkBefore": ink_before, "inkAfter": ink_after,
                "message": f"已收到停止请求，撤销 {clicks} 次后中止",
            }

        if not cleared:
            still_dirty = (
                final_image is not None
                and not self._is_canvas_blank(
                    final_image, region, self._calibration_now(project_id, calibration)
                )
            )
            if still_dirty:
                raise TaskError(
                    409, "E_CLEAR_FAILED",
                    "撤销历史已见底但画布仍有残留（撤销深度不足，且橡皮兜底不可用或"
                    "未能涂净）；请在小程序画板上手动清空后重试",
                )

        # 清空成功：底图可能已变（撤销掉的东西改变了常驻背景差异），
        # 基线整幅翻转即作废，下次判定命中空白时自动重学。
        if final_image is not None:
            refreshed = self._calibration_now(project_id, calibration)
            if canvas_blankness.baseline_is_stale(
                final_image, region, refreshed.blank_baseline
            ):
                self._persist_blank_baseline(refreshed, None)
        message = f"已清空画布（撤销 {clicks} 次）"
        if method == "undo+eraser":
            message += "，橡皮涂净残留"
        if switch_warning:
            message += f"；{switch_warning}"
        return {
            "cleared": True, "method": method,
            "inkBefore": ink_before, "inkAfter": ink_after,
            "message": message,
        }

    def _calibration_now(self, project_id: str, fallback: CanvasProfile) -> CanvasProfile:
        """撤销循环可能触发过基线学习/作废，最新记录读不到时退回旧值。"""
        return self._store.latest_calibration(project_id) or fallback

    # ---------- 一键自动校准（校准页专用） ----------

    def auto_calibrate(
        self,
        project_id: str,
        *,
        hwnd: int | None = None,
        confirm_dirty_probe: bool = False,
        targets: list[str] | None = None,
    ) -> dict:
        """校准页一键自动校准：画布识别 + 工具条行为探测（含收起态重扫）。

        同步执行并占用全局执行槽（与 clear_canvas 同范式），步骤经内存事件
        总线推给 SSE。``targets`` 限定单独识别范围（None/含 "all" = 全部）；
        非全部时未选中的控件以既有坐标作人工兜底喂给探测流程、天然跳过，
        只重扫选中项。脏画布默认阻断，经 ``confirm_dirty_probe`` 授权后允许
        盲点探测（与清空画布脏探测同一授权边界）。
        """
        if self._store.get_project(project_id) is None:
            raise TaskError(404, "E_NOT_FOUND", "项目不存在")
        with self._lock:
            existing = self._cal_runs.get(project_id)
            if existing is not None and not existing.done:
                raise TaskError(409, "E_TASK_CONFLICT", "该项目已有自动校准在执行")
            self._cal_runs.pop(project_id, None)

        target = self._auto_target(project_id, hwnd)
        handle, mouse = self._claim(target)
        run = _CalibrationRun(handle=handle)
        with self._lock:
            self._cal_runs[project_id] = run
        try:
            # 探测期间程序持续接管鼠标，ESC/空格随时可急停（与绘制期同一急停键）。
            self._start_esc_watch(mouse, target, handle)
            return self._auto_calibrate_run(
                project_id, run, mouse, target,
                confirm_dirty_probe=confirm_dirty_probe,
                targets=targets,
            )
        finally:
            handle.guard_event.set()
            try:
                mouse.up()  # type: ignore[attr-defined]
            except Exception:
                pass
            self._release(handle)
            with self._lock:
                run.done = True
                run.finished_at = time.monotonic()

    def _auto_target(self, project_id: str, hwnd: int | None) -> DrawingTarget:
        """自动校准目标：有既有校准走常规解析（含标题重绑）；全新项目须由前端带选窗口 hwnd。"""
        if self._store.latest_calibration(project_id) is not None:
            return self._target_for_project(project_id)
        if hwnd is None:
            raise TaskError(
                422, "E_CANVAS_MISSING",
                "项目尚未校准，请先在校准页选择目标窗口，再发起一键自动校准",
            )
        if not self._device.alive(hwnd):
            raise TaskError(409, "E_WINDOW_LOST", "目标窗口已关闭")
        return Win32Target(hwnd, self._device, self._probe)

    def calibration_stop(self, project_id: str) -> None:
        """请求中止进行中的一键自动校准（探测线程在下一检查点落 E_STOPPED）。"""
        with self._lock:
            run = self._cal_runs.get(project_id)
        if run is not None and not run.done:
            run.handle.request_stop("user")

    def _start_esc_watch(
        self, mouse: object, target: DrawingTarget, handle: _RunHandle
    ) -> None:
        """校准/清空等非绘制流程的急停监听：ESC/空格即刻置位 stop_event。

        guard_active 未置位时 evaluate_signals 只评估 ESC/空格，与绘制期同一
        急停键语义；顺带消费左键边位，防注入点击的陈旧按下位泄漏到后续任务。
        线程靠 handle.guard_event 停止，调用方收尾时必须 set。
        """
        try:
            self._probe.clear_escape_state()
            mouse.clear_escape_state()  # type: ignore[attr-defined]
        except Exception:
            pass

        def poll() -> None:
            while not handle.guard_event.is_set():
                try:
                    self._probe.left_button_pressed()
                    reason = evaluate_signals(
                        handle, self._probe, mouse, target, 0.0, False
                    )
                except Exception:
                    time.sleep(0.05)
                    continue
                if reason is not None:
                    handle.request_stop(reason)
                    return
                time.sleep(0.05)

        threading.Thread(target=poll, daemon=True).start()

    def calibration_events(self, project_id: str, since: int) -> tuple[list[dict], bool]:
        """SSE 游标轮询：返回 (seq > since 的事件, 是否仍在进行)。

        终态 run 保留一小段窗口供慢客户端补尾，超期惰性清理；不存在或已清理
        返回 ([], False)，端点据此结束流。
        """
        with self._lock:
            run = self._cal_runs.get(project_id)
            if run is None:
                return [], False
            fresh = [dict(event) for event in run.events[since:]]
            if run.done and time.monotonic() - run.finished_at > _CAL_RUN_RETAIN_S:
                self._cal_runs.pop(project_id, None)
                return fresh, False
            return fresh, not run.done

    def _auto_calibrate_run(
        self,
        project_id: str,
        run: _CalibrationRun,
        mouse: object,
        target: DrawingTarget,
        *,
        confirm_dirty_probe: bool,
        targets: list[str] | None,
    ) -> dict:
        """事件发布外壳：把探测步骤翻译成 SSE 事件序列，终态发 done/failed。"""

        def publish(event_type: str, payload: dict) -> None:
            with self._lock:
                run.events.append({
                    "seq": len(run.events) + 1,
                    "type": event_type,
                    "payload": payload,
                })

        completed: list[str] = []

        def emit_step(event: dict) -> None:
            state = event.get("state")
            if state == STATE_MANUAL:
                return  # 人工兜底项前端已有既有标注，不必重复推送
            publish("calibration.warning", step_to_warning(event))
            geometry = event.get("geometry")
            if geometry is not None and state == STATE_DONE:
                completed.append(str(event.get("step")))
                publish("calibration.geometry", {
                    "step": event.get("step"),
                    "geometry": geometry,
                })

        try:
            result = self._auto_calibrate_impl(
                project_id, run.handle, mouse, target,
                confirm_dirty_probe=confirm_dirty_probe,
                targets=targets,
                emit_step=emit_step,
            )
        except TaskError as exc:
            publish("calibration.failed", {"code": exc.code, "message": exc.message})
            raise
        except Exception as exc:
            publish("calibration.failed", {
                "code": "E_CALIBRATION_FAILED",
                "message": f"自动校准异常：{exc.__class__.__name__}",
            })
            raise TaskError(409, "E_CALIBRATION_FAILED", f"自动校准异常：{exc}") from exc
        result["steps"] = completed
        publish("calibration.done", result)
        return result

    def _auto_calibrate_impl(
        self,
        project_id: str,
        handle: _RunHandle,
        mouse: object,
        target: DrawingTarget,
        *,
        confirm_dirty_probe: bool,
        targets: list[str] | None,
        emit_step: Callable[[dict], None],
    ) -> dict:
        def check_stop() -> None:
            if handle.stop_event.is_set():
                raise TaskError(409, "E_STOPPED", "自动校准已中止")

        previous = self._store.latest_calibration(project_id)
        want_all = targets is None or "all" in targets
        want = (lambda name: True) if want_all else (lambda name: name in targets)
        # 单独识别非画布目标时必须有既有校准做窗口与其余字段的锚点。
        if previous is None and not want("canvas"):
            raise TaskError(422, "E_CANVAS_MISSING", "尚未完成画布校准，请先识别画布")

        check_stop()
        shot = target.capture()
        image = shot.image
        if image is None or not target.alive():
            raise TaskError(409, "E_WINDOW_LOST", target.lost_hint)
        image_h, image_w = int(image.shape[0]), int(image.shape[1])
        client = target.client_area()

        # 1) 画布自动识别；未选中画布时沿用既有校准的画布矩形（同帧）。
        if want("canvas"):
            found = detect_canvas_rect(image)
            if found is None:
                # 面板展开态把画布挤矮成近方形（真机实测 ratio≈0.80），严格档
                # （0.78，与 v6 对齐）直接拒检——用户开着面板点「一键自动校准」
                # 就会吃 E_CANVAS_NOT_FOUND。严格空手才放宽宽/高比重试一次。
                found = detect_canvas_rect(image, max_ratio=0.85)
            if found is None:
                emit_step({
                    "step": STEP_CANVAS, "state": STATE_FAILED,
                    "geometry": None, "evidence": "当前画面未识别到白色画布区域",
                })
                raise TaskError(422, "E_CANVAS_NOT_FOUND", "当前画面未识别到画布，请让画板窗口完整可见后重试")
            left, top, right, bottom = found
            canvas = Rect(left, top, right - left, bottom - top)
        else:
            assert previous is not None
            canvas = Rect(
                previous.rect_x, previous.rect_y, previous.rect_w, previous.rect_h
            )
        emit_step({
            "step": STEP_CANVAS, "state": STATE_DONE,
            "geometry": _rect_geometry_of(canvas), "evidence": "",
        })
        check_stop()

        # 2) 脏画布门禁：盲点候选可能命中「清空/换色」等不可恢复控件。
        region = (canvas.x, canvas.y, canvas.w, canvas.h)
        blank = (
            self._is_canvas_blank(image, region, previous)
            if previous is not None
            else canvas_blankness.is_blank(image, region, None)
        )
        if not blank and not confirm_dirty_probe:
            raise TaskError(
                409, "E_CANVAS_DIRTY",
                "画布上已有笔迹，自动校准会盲点画板工具条、可能误触清空类控件。"
                "请先「清空画布」后再自动校准",
            )

        # 3) 快照：校准页标注层锚定这一帧；存失败只是没得标注，不阻断校准。
        snapshot_id = self._store.new_snapshot_id()
        try:
            snapshot_file = self._store.snapshot_path(project_id, snapshot_id)
            snapshot_file.parent.mkdir(parents=True, exist_ok=True)
            Image.fromarray(np.asarray(image, dtype=np.uint8)).save(
                snapshot_file, format="JPEG", quality=90,
            )
            snapshot_id_out: str | None = snapshot_id
        except Exception:
            snapshot_id_out = None

        # 4) 工具条行为探测（含收起态重扫）。未选中项作人工兜底、天然跳过。
        probe_targets = self._auto_probe_targets(want_all, want)
        dx = float(canvas.x - previous.rect_x) if previous is not None else 0.0
        dy = float(canvas.y - previous.rect_y) if previous is not None else 0.0
        tools: ToolBitmap | None = None
        if probe_targets:
            manual = self._auto_manual_bitmap(
                previous, dx, dy, want_all, set(probe_targets)
            )
            prober = ToolbarProber(
                probe=target,
                mouse=mouse,
                to_screen=lambda x, y: image_point_to_screen(
                    x, y, image_w, image_h, client
                ),
            )
            try:
                tools = prober.run(
                    canvas=region,
                    band=self._probe_band_rect(canvas),
                    open_bitmap=manual,
                    on_step=emit_step,
                    should_stop=handle.stop_event.is_set,
                    rescan_collapsed=want_all or STEP_FOLD in probe_targets,
                )
            except ProbeError as exc:
                raise TaskError(409, exc.code, exc.message) from exc

        # 5) 落库：坐标与本次识别的 rect 同帧（dx=dy=0），无需回移。
        base = previous or self._auto_base_profile(
            project_id, target, canvas, shot.method
        )
        if tools is not None:
            self._persist_probed_calibration(
                project_id, base, tools, 0.0, 0.0,
                rect=canvas,
                window_title=self._device.title(base.hwnd),
                capture_method=shot.method,
                snapshot_id=snapshot_id_out,
                frame_w=image_w,
                frame_h=image_h,
            )
        else:
            # 仅重识别画布：既有工具坐标随画布平移到新帧，基线随换帧作废。
            self._store.save_calibration(
                project_id=project_id,
                hwnd=base.hwnd,
                window_title=self._device.title(base.hwnd),
                capture_method=shot.method,
                rect_x=canvas.x,
                rect_y=canvas.y,
                rect_w=canvas.w,
                rect_h=canvas.h,
                dpi_scale=self._device.dpi_scale(base.hwnd),
                source=base.source,
                confidence=base.confidence,
                slider=self._shift_rect(base.slider, dx, dy),
                undo=self._shift_point(base.undo, dx, dy),
                fold=self._shift_point(base.fold, dx, dy),
                tools_open=self._shifted_bitmap(base.tools_open, dx, dy),
                tools_collapsed=self._shifted_bitmap(base.tools_collapsed, dx, dy),
                device_kind=base.device_kind,
                device_serial=base.device_serial,
                blank_baseline=None,
                snapshot_id=snapshot_id_out,
                frame_w=image_w,
                frame_h=image_h,
            )

        saved = self._store.latest_calibration(project_id)
        return {
            "calibrationId": saved.id if saved is not None else None,
            "snapshotId": snapshot_id_out,
            "rect": canvas.as_dict(),
            # 本次校准帧尺寸：前端标注层据此对齐坐标系，避免回退实时帧致缩放失真
            "imageWidth": image_w,
            "imageHeight": image_h,
            "collapsedCaptured": bool(tools is not None and tools.collapsed is not None),
            "message": "自动校准完成",
        }

    @staticmethod
    def _auto_probe_targets(want_all: bool, want: Callable[[str], bool]) -> list[str]:
        names = [STEP_FOLD, STEP_SLIDER, STEP_UNDO, STEP_ERASER, STEP_BRUSH]
        if want_all:
            return names
        selected = [name for name in names if want(name)]
        # 单独识别滑杆没有意义（轨道由折叠/滑杆流程顺带产出），并入折叠目标。
        if STEP_SLIDER in selected and STEP_FOLD not in selected:
            selected.insert(0, STEP_FOLD)
        return selected

    def _auto_manual_bitmap(
        self,
        previous: CanvasProfile | None,
        dx: float,
        dy: float,
        want_all: bool,
        probe_targets: set[str],
    ) -> ToolBitmap | None:
        """单独识别的人工兜底：未选中项沿用既有坐标（平移到当前帧），选中项置 None 强制重扫。"""
        if want_all or previous is None:
            return None
        open_raw = previous.tools_open or {}

        def pixel(name: str, legacy=None) -> tuple[float, float] | None:
            if name in probe_targets:
                return None
            point = self._shift_point(open_raw.get(name) or legacy, dx, dy)
            return (point["x"], point["y"]) if point is not None else None

        slider = None
        if STEP_SLIDER not in probe_targets:
            slider = self._shift_rect(open_raw.get("slider") or previous.slider, dx, dy)
        keep_brush = STEP_BRUSH not in probe_targets
        return ToolBitmap(
            brush=pixel(STEP_BRUSH),
            eraser=pixel(STEP_ERASER),
            slider=slider,
            undo=pixel(STEP_UNDO, previous.undo),
            fold=pixel(STEP_FOLD, previous.fold),
            brush_selected_lum=open_raw.get("brush_selected_lum") if keep_brush else None,
            brush_idle_lum=open_raw.get("brush_idle_lum") if keep_brush else None,
        )

    def _auto_base_profile(
        self, project_id: str, target: DrawingTarget, canvas: Rect, capture_method: str
    ) -> CanvasProfile:
        """全新项目尚无校准时，给持久化路径提供窗口字段锚点。"""
        hwnd = int(getattr(target, "hwnd", 0))
        return CanvasProfile(
            id="",
            project_id=project_id,
            hwnd=hwnd,
            window_title=self._device.title(hwnd),
            capture_method=capture_method,
            rect_x=canvas.x,
            rect_y=canvas.y,
            rect_w=canvas.w,
            rect_h=canvas.h,
            dpi_scale=self._device.dpi_scale(hwnd),
            source="auto",
            confidence=1.0,
            captured_at=utc_now(),
        )

    def _clear_undo_point(
        self,
        calibration: CanvasProfile,
        dx: float,
        dy: float,
    ) -> dict | None:
        """撤销点击坐标：探测位图优先、手动标定其次；双缺返回 None 由调用方分流。"""
        candidates = [
            p
            for p in (
                self._shift_point(
                    self._point_dict((calibration.tools_open or {}).get("undo")), dx, dy
                ),
                self._shift_point(self._point_dict(calibration.undo), dx, dy),
            )
            if p is not None
        ]
        return candidates[0] if candidates else None

    def _undo_until_blank(
        self,
        calibration: CanvasProfile,
        first_image,
        region: tuple[int, int, int, int],
        point: dict,
        guard_canvas: Rect,
        *,
        image_size: tuple[int, int],
        client: object,
        mouse: object,
        target: DrawingTarget,
        handle: _RunHandle,
    ) -> tuple[bool, int, int, object]:
        """连点撤销直到空白 / 预算耗尽 / 连续 plateau（撤到底）。

        返回 (是否已空白, 点击次数, 最后一帧墨量, 最后一帧截图)。每次点击都
        截图复核；plateau 需连续 CLEAR_PLATEAU_CLICKS 次墨量几乎不动才认
        「撤到底」，以容忍小程序撤销动画的截图中途态。
        """
        ink = ink_pixels(first_image, region)
        plateau = 0
        image = first_image
        clicks = 0
        while clicks < CLEAR_UNDO_MAX_CLICKS and not handle.stop_event.is_set():
            if not self._click_image_point(
                mouse, target, (point["x"], point["y"]), image_size, client,
                canvas=guard_canvas,
            ):
                raise TaskError(
                    409, "E_CLEAR_FAILED",
                    "撤销按钮坐标落在画布内（画布可能已漂移），已停止清空；请重新校准",
                )
            clicks += 1
            if handle.stop_event.wait(CLEAR_SETTLE_S):
                break
            image = target.capture_image()
            if image is None:
                raise TaskError(409, "E_CLEAR_FAILED", "清空过程中的复检截图失败")
            now = ink_pixels(image, region)
            if self._is_canvas_blank(image, region, calibration):
                return True, clicks, now, image
            plateau = plateau + 1 if abs(now - ink) < UNDO_EFFECT_MIN_PIXELS else 0
            ink = now
            if plateau >= CLEAR_PLATEAU_CLICKS:
                break
        return False, clicks, ink, image

    def _widest_width_px(self, project_id: str) -> float | None:
        """该项目最近任务标定出的最粗笔宽（px）；未标定过返回 None。"""
        try:
            task = self._store.latest_task_for_project(project_id)
        except Exception:
            return None
        if task is None:
            return None
        try:
            table = ExecParams.from_dict(task.exec_params).width_table
        except Exception:
            return None
        if not table:
            return None
        return max(width for _frac, width in table)

    def _eraser_sweep(
        self,
        eraser: tuple[float, float],
        brush: tuple[float, float],
        tools: ToolBitmap,
        slider: dict,
        region: tuple[int, int, int, int],
        image,
        guard_canvas: Rect,
        *,
        image_size: tuple[int, int],
        client: object,
        mouse: object,
        target: DrawingTarget,
        handle: _RunHandle,
        calibration: CanvasProfile,
        widest_px: float | None,
    ) -> bool:
        """橡皮兜底（clear-and-guide-design §4.4）：最粗档 + 行扫涂净撤销残留。

        本方法是全设计唯一允许在画布内落笔的流程，且只在确认橡皮态后执行。
        首行后墨量不减反增 = 橡皮坐标错位画上了墨，立即中止（调用方切回画笔）。
        返回是否涂净；无论成败，调用方都必须负责切回画笔态。
        """
        # 步 2：笔宽置最粗档。与 PenCalibrator 同语义在轨道右端内缩点击。
        inset = PenCalibrator.TRACK_EDGE_INSET
        right = slider["x"] + slider["w"] * (1.0 - inset)
        center_y = slider["y"] + slider["h"] / 2.0
        self._click_image_point(
            mouse, target, (right, center_y), image_size, client, canvas=guard_canvas
        )

        # 步 3：切橡皮并校验。有完整选中指纹时必须确认已离开画笔态（注意
        # _brush_selected 对部分指纹恒放行 True，不可拿它当「无指纹」信号）；
        # 无指纹时盲信坐标——坐标错位的风险由首行墨量复查拦住。
        has_fingerprint = (
            tools.brush_selected_lum is not None and tools.brush_idle_lum is not None
        )
        if not self._click_image_point(
            mouse, target, eraser, image_size, client, canvas=guard_canvas
        ):
            return False
        handle.stop_event.wait(CLEAR_SETTLE_S)
        if has_fingerprint and self._brush_selected(tools, target):
            return False

        x, y, w, h = region
        row_w = max(widest_px or 0.0, ERASER_FALLBACK_WIDTH_PX)
        step = max(1.0, row_w * 0.5)
        top = y + ERASER_ROW_EDGE_INSET_PX
        bottom = y + h - ERASER_ROW_EDGE_INSET_PX
        left = x - ERASER_ROW_OVERSCAN_PX
        right_x = x + w + ERASER_ROW_OVERSCAN_PX

        # 步 4-5：行扫涂（左右往返），扫完复查；仍有墨且预算内再扫一轮。
        to_screen = lambda px, py: image_point_to_screen(  # noqa: E731
            px, py, image_size[0], image_size[1], client
        )
        ink = ink_pixels(image, region)
        for round_no in range(ERASER_MAX_ROUNDS):
            row_y = top
            index = 0
            while row_y <= bottom and not handle.stop_event.is_set():
                a, b = (left, right_x) if index % 2 == 0 else (right_x, left)
                mouse.drag_polyline(  # type: ignore[attr-defined]
                    [to_screen(a, row_y), to_screen(b, row_y)],
                    should_stop=handle.stop_event.is_set,
                )
                row_y += step
                index += 1
                if round_no == 0 and index == 1:
                    # 首行后立刻复查：坐标错位时这一行画的是墨，必须马上停。
                    handle.stop_event.wait(CLEAR_SETTLE_S)
                    check = target.capture_image()
                    if check is None:
                        return False
                    first_ink = ink_pixels(check, region)
                    if first_ink > ink:
                        return False
                    ink = first_ink
            if handle.stop_event.is_set():
                return False
            handle.stop_event.wait(CLEAR_SETTLE_S)
            check = target.capture_image()
            if check is None:
                return False
            if self._is_canvas_blank(check, region, calibration):
                return True
        return False

    def _switch_back_to_brush(
        self,
        brush: tuple[float, float],
        tools: ToolBitmap,
        guard_canvas: Rect,
        *,
        image_size: tuple[int, int],
        client: object,
        mouse: object,
        target: DrawingTarget,
    ) -> str:
        """扫涂结束切回画笔态（§4.4 步 6）；校验失败返回要拼进结果的警告语。"""
        self._click_image_point(
            mouse, target, brush, image_size, client, canvas=guard_canvas
        )
        time.sleep(CLEAR_SETTLE_S)
        has_fingerprint = (
            tools.brush_selected_lum is not None and tools.brush_idle_lum is not None
        )
        if has_fingerprint and not self._brush_selected(tools, target):
            return "橡皮已用但切回画笔校验失败，请目视确认已切回画笔"
        return ""

    def stop(self, task_id: str) -> TaskRecord:
        record = self._store.get_task(task_id)
        if record is None:
            raise TaskError(404, "E_NOT_FOUND", "任务不存在")
        if record.status in TERMINAL_STATUSES:
            raise TaskError(409, "E_TASK_CONFLICT", "任务已结束")
        with self._lock:
            handle = self._handle
        if handle is not None:
            handle.request_stop("user")
        # 仅 DRAWING 显式进入 STOPPING；ARMING 由执行线程直接落 STOPPED（design 6.1）。
        self._store.mark_stopping(task_id)
        record = self._store.get_task(task_id)
        assert record is not None
        return record

    def busy(self) -> bool:
        with self._lock:
            return self._handle is not None

    # ---------- 内部：槽位与校验 ----------

    def _target(self, plan: PlanRecord) -> DrawingTarget:
        """由最新校准记录解析绘制目标（pc / mirror 按 device_kind 分发）。"""
        return self._target_for_project(plan.project_id)

    def _target_for_project(self, project_id: str) -> DrawingTarget:
        calibration = self._store.latest_calibration(project_id)
        if calibration is None or calibration.rect_w <= 0 or calibration.rect_h <= 0:
            raise TaskError(422, "E_CANVAS_MISSING", "项目尚未完成画布校准")
        target = resolve_target(calibration, self._device, self._probe)
        if not target.alive() and calibration.window_title:
            # 小程序重启后句柄会变：按窗口标题找回同名唯一存活窗口自动重绑。
            # 画布矩形漂移交给落笔前的漂移校验兜底，不在此处放宽。
            hwnd = find_live_hwnd_by_title(self._device, calibration.window_title)
            if hwnd is not None:
                target = type(target)(hwnd, self._device, self._probe)
        return target

    def _claim(self, target: DrawingTarget) -> tuple[_RunHandle, object]:
        """占用全局唯一执行槽位并构造鼠标；失败抛 TaskError。"""
        with self._lock:
            if self._handle is not None:
                raise TaskError(409, "E_TASK_CONFLICT", "已有绘制任务在执行")
            if not target.alive():
                raise TaskError(409, "E_WINDOW_LOST", target.lost_hint)
            try:
                mouse = self._mouse_factory()
            except TaskError:
                raise
            except Exception as exc:
                raise TaskError(503, "E_EXECUTOR_UNAVAILABLE", "执行器不可用") from exc
            handle = _RunHandle()
            self._handle = handle
        return handle, mouse

    def _release(self, handle: _RunHandle) -> None:
        # 只释放自己这一份，避免误清新任务的槽位。
        with self._lock:
            if self._handle is handle:
                self._handle = None

    def _arm(self, task_id: str) -> None:
        self._store.update_task(task_id, status="ARMING", started_at=utc_now())
        self._emit(task_id, "task.state", {"status": "ARMING"})

    def _load_paths(self, plan: PlanRecord) -> list[list[tuple[float, float]]]:
        raw = json.loads(Path(plan.paths_path).read_text(encoding="utf-8"))
        # 空 path 不占笔序：先过滤再算 total，避免 total 与扁平后的实际笔数错位。
        return [
            [(float(x), float(y)) for x, y in path]
            for path in raw
            if path
        ]

    def _spawn(
        self,
        record: TaskRecord,
        plan: PlanRecord,
        paths: list[list[tuple[float, float]]],
        params: ExecParams,
        mouse: object,
        target: DrawingTarget,
        countdown_ms: int,
        allow_drift: float,
        handle: _RunHandle,
        cursor: int = 0,
    ) -> None:
        threading.Thread(
            target=self._run,
            args=(record.id, plan, paths, params, mouse, target, countdown_ms, allow_drift, cursor, handle),
            daemon=True,
        ).start()

    def _transition(self, task_id: str, status: str, **db_fields: object) -> None:
        self._store.update_task(task_id, status=status, **db_fields)
        payload: dict[str, object] = {"status": status}
        if db_fields.get("error_code"):
            payload["errorCode"] = db_fields["error_code"]
        if db_fields.get("stop_reason"):
            payload["stopReason"] = db_fields["stop_reason"]
        self._emit(task_id, "task.state", payload)

    def _emit(self, task_id: str, event_type: str, payload: dict) -> None:
        self._store.append_event(task_id, event_type, payload)

    def _emit_countdown_tick(self, task_id: str, remaining_s: float) -> None:
        self._emit(task_id, "task.countdown", {"remainingSec": max(0, int(-(-remaining_s // 1)))})

    # ---------- 内部：执行线程 ----------

    def _run(
        self,
        task_id: str,
        plan: PlanRecord,
        paths: list[list[tuple[float, float]]],
        params: ExecParams,
        mouse: object,
        target: DrawingTarget,
        countdown_ms: int,
        allow_drift: float,
        cursor: int,
        handle: _RunHandle,
    ) -> None:
        started = time.monotonic()
        total = len(paths)
        self._start_guard(task_id, mouse, target, params, handle)
        try:
            self._arm_and_draw(
                task_id, plan, paths, params, mouse, target,
                countdown_ms, allow_drift, cursor, total, started, handle,
            )
        except TaskError as exc:
            self._finish_failed(task_id, exc.code, exc.message, self._cursor(task_id), total, started)
        except Exception as exc:  # 输入异常等未分类错误统一转 FAILED
            self._finish_failed(task_id, "E_EXECUTOR_UNAVAILABLE", str(exc), self._cursor(task_id), total, started)
        finally:
            handle.guard_event.set()
            try:
                mouse.up()  # type: ignore[attr-defined]
            except Exception:
                pass
            self._release(handle)

    def _arm_and_draw(
        self,
        task_id: str,
        plan: PlanRecord,
        paths: list[list[tuple[float, float]]],
        params: ExecParams,
        mouse: object,
        target: DrawingTarget,
        countdown_ms: int,
        allow_drift: float,
        cursor: int,
        total: int,
        started: float,
        handle: _RunHandle,
    ) -> None:
        calibration = self._store.latest_calibration(plan.project_id)
        assert calibration is not None

        # 倒计时允许用户切到目标窗口，可被停止打断（ARMING → STOPPED）；
        # 末段目标不在前台时冻结等待（G1），而非直接失败。
        if self._guarded_countdown(task_id, max(0, countdown_ms) / 1000, target, handle) == "stopped":
            self._finish_stopped(task_id, cursor, total, started, handle)
            return

        if not target.alive():
            raise TaskError(409, "E_WINDOW_LOST", "倒计时期间目标窗口丢失")
        if not target.foreground_matches():
            raise TaskError(409, "E_FOREGROUND_MISMATCH", "前台窗口与目标不一致，未落笔")

        try:
            capture = target.capture()
        except CaptureError as exc:
            raise TaskError(422, "E_CAPTURE_FAILED", "落笔前截图失败") from exc

        image_h, image_w = capture.image.shape[:2]
        image_size = (image_w, image_h)
        detected = detect_canvas_rect(capture.image)
        if detected is None:
            raise TaskError(409, "E_CANVAS_DRIFT", "落笔前未识别到画布，未落笔")
        l, t, r, b = detected
        new_rect = Rect(l, t, r - l, b - t)
        saved_rect = Rect(
            calibration.rect_x, calibration.rect_y, calibration.rect_w, calibration.rect_h
        )
        drift = 1.0 - _rect_iou(new_rect, saved_rect)
        if drift > allow_drift and _is_panel_state_change(saved_rect, new_rect):
            # 面板开合导致的合法高度变化：按当前帧继续，仅提示。
            self._emit(task_id, "task.warning", {
                "message": f"工具面板处于{'展开' if new_rect.h < saved_rect.h else '收起'}态，"
                           f"画布高度与校准记录不同（{drift:.1%}），已按当前画面继续"
            })
            drift = 0.0
        if drift > allow_drift:
            raise TaskError(409, "E_CANVAS_DRIFT", f"画布位置变化 {drift:.1%} 超过允许值")
        if drift > allow_drift * 0.5:
            self._emit(task_id, "task.warning", {
                "message": f"画布位置偏移 {drift:.1%}，接近允许阈值 {allow_drift:.1%}"
            })

        client = target.client_area()

        # 控件坐标来源是校准坐标系：按画布矩形整体平移（同一截图坐标系）。
        dx, dy = new_rect.x - saved_rect.x, new_rect.y - saved_rect.y
        slider_legacy = self._shift_rect(calibration.slider, dx, dy)
        undo_legacy = self._shift_point(calibration.undo, dx, dy)
        fold_legacy = self._shift_point(calibration.fold, dx, dy)

        # 双态工具位图：展开态/折叠态各一套坐标，绝不跨态使用；两者皆无即旧
        # 校准（单套坐标、无法校验画笔态）。
        tools_open = self._tool_bitmap(calibration.tools_open, dx, dy)
        tools_collapsed = self._tool_bitmap(calibration.tools_collapsed, dx, dy)
        legacy_mode = tools_open is None and tools_collapsed is None
        slider_open = tools_open.slider if tools_open is not None else slider_legacy
        undo_open = self._point_dict(tools_open.undo if tools_open else None) or undo_legacy
        fold_open = self._point_dict(tools_open.fold if tools_open else None) or fold_legacy
        # 「折叠」「展开」是两个独立按钮：tools_collapsed.fold 存的是收起态生效
        # 的「展开」钮，只用于开面板；tools_open.fold 是展开态生效的「折叠」钮，
        # 只用于关面板。旧校准没有展开钮时回退用折叠钮坐标（可逆 UI 原位生效）。
        fold_closed = self._point_dict(tools_collapsed.fold if tools_collapsed else None) or fold_open

        panel = self._make_panel(
            params, target, mouse, image_size, client, slider_open, fold_open,
            handle.stop_event,
        )
        # 面板当前态：None 表示未知（旧校准无折叠按钮，坐标直接可用）。
        panel_open: bool | None = None
        if panel is not None and slider_open is not None:
            panel_open = panel.is_open(slider_open)

        rendering_calibration = params.auto_width and params.width_table is None and cursor == 0

        # 探测在标定之前跑：标定依赖滑杆坐标，而旧校准（尤其全新用户只剩画布
        # 矩形）可能没有任何人工锚点。探测与自动变粗解耦（clear-and-guide-design
        # §6.1）：任何模式首画且尚无双态位图都探测——撤销/折叠/画笔坐标的价值
        # 独立于笔宽。空白门禁保留：脏画布跳过并推结构化提示码，引导走「清空
        # 画布」流程（用户确认墨可弃后经 clear-canvas 的脏探测分支解锁）。
        # 失败只警告、沿用旧单套坐标，不阻断绘制。
        probe_needed = cursor == 0 and calibration.tools_open is None
        # 收起态位图缺失（如「复制的校准」只带 tools_open）时按需补学「展开」钮：
        # 自动变粗的标定与换宽都要先展开面板，缺展开钮会退化单细笔 + 跳过画笔校验。
        # 重扫会画测试线，只在空白画布上做；失败沿用旧路径、不阻断绘制。
        rescan_needed = (
            cursor == 0
            and params.auto_width
            and calibration.tools_open is not None
            and calibration.tools_collapsed is None
            and panel_open is False
        )
        canvas_blank: bool | None = None
        if cursor == 0 and (params.auto_width or probe_needed or rescan_needed):
            canvas_blank = self._canvas_is_blank(target, new_rect, calibration)
        probed_now = False
        if probe_needed:
            if canvas_blank is False:
                # 画布已有墨迹时盲点下方候选可能命中「清空」类不可恢复控件，
                # 宁可跳过探测、退化为单细笔/旧路径，也不冒险毁掉用户画作。
                self._emit(task_id, "task.warning", {
                    "message": "画布上已有笔迹，已跳过工具条自动探测（盲点候选可能误触清空类控件）。"
                               "可点「清空画布，从头绘制」（旧墨全部丢弃后自动学习并开始绘制）；"
                               "或到校准页手动框选粗细滑杆轨道与撤销按钮",
                    "code": "probe_skipped_dirty",
                })
            else:
                probed = self._probe_toolbar(
                    task_id, plan.project_id, calibration, target, mouse, client, image_size,
                    new_rect, dx, dy, fold_closed, slider_open, undo_open, handle,
                )
                if handle.stop_event.is_set():
                    self._finish_stopped(task_id, cursor, total, started, handle)
                    return
                if probed is not None:
                    probed_now = True
                    tools_open = self._tool_bitmap(asdict(probed), 0.0, 0.0)
                    slider_open = tools_open.slider if tools_open is not None else slider_open
                    undo_open = self._point_dict(tools_open.undo) if tools_open else undo_open
                    fold_open = (
                        self._point_dict(tools_open.fold) if tools_open else None
                    ) or fold_open
                    fold_closed = (
                        self._point_dict(tools_collapsed.fold) if tools_collapsed else None
                    ) or fold_open
                    legacy_mode = False
                    # 探测过程中会开合面板，状态未知，按实际截图重判。
                    panel_open = None
                    # 旧校准的 panel 可能已存在，但其滑杆/折叠坐标已被探测结果替换，
                    # 必须按新坐标重建。
                    panel = self._make_panel(
                        params, target, mouse, image_size, client, slider_open,
                        fold_open, handle.stop_event,
                    )

        if rescan_needed and not handle.stop_event.is_set():
            if canvas_blank is False:
                self._emit(task_id, "task.warning", {
                    "message": "画布上已有笔迹，已跳过工具面板收起态重扫（未学到「展开」按钮，"
                               "本次可能回退单细笔）；点「清空画布，从头绘制」后会自动补学",
                    "code": "probe_skipped_dirty",
                })
            else:
                probed = self._probe_toolbar(
                    task_id, plan.project_id, calibration, target, mouse, client, image_size,
                    new_rect, dx, dy, fold_closed, slider_open, undo_open, handle,
                    manual=tools_open, rescan_collapsed=True,
                )
                if handle.stop_event.is_set():
                    self._finish_stopped(task_id, cursor, total, started, handle)
                    return
                if probed is not None and probed.collapsed is not None:
                    # 收起态位图（含「展开」钮）在本次截图帧；面板已恢复展开态。
                    tools_collapsed = probed.collapsed
                    fold_closed = self._point_dict(tools_collapsed.fold) or fold_open
                    # 重扫会开合面板，状态按实际截图重判（下游 unfold 会先查态）。
                    panel_open = None

        if probed_now and not params.auto_width and slider_open is not None and fold_open is not None:
            # 探测以展开态收尾；单细笔模式没有后续 panel_active 收起流程，
            # 展开的面板会一直遮住画布下部，这里就地折回（尽力而为）。
            foldback = PanelController(
                probe=target,
                mouse=mouse,
                to_screen=lambda x, y: image_point_to_screen(
                    x, y, image_size[0], image_size[1], client
                ),
                stopped=handle.stop_event.is_set,
                sleep=handle.stop_event.wait,
            )
            try:
                if foldback.is_open(slider_open) is True:
                    foldback.fold(slider_open, self._button_chain(fold_open))
            except Exception:
                pass

        if rendering_calibration and not legacy_mode:
            # 标定会用测试笔在刻度带试画；若当前停在橡皮态，试画会先擦掉用户画作。
            # 有双态位图时把画笔态校验前移到标定之前（折叠态同样按跳过规则处理）。
            self._ensure_brush_before_draw(
                task_id, tools_open, tools_collapsed, panel, slider_open,
                fold_open, fold_closed, panel_open, mouse, target, image_size, client,
                canvas=new_rect,
            )

        # 自动变粗：首绘先用行为探测补齐控件坐标（上方），再标定笔宽表（刻度带
        # 测试线→清空画布），续画复用已回写的宽度表，保证分桶笔序与首绘逐次一致。
        if rendering_calibration:
            if slider_open is None:
                self._emit(task_id, "task.warning", {
                    "message": "未标定粗细滑杆，已回退单细笔绘制"
                               "（清空画布后重新开始将自动探测，或到校准页框选滑杆轨道）"
                })
            elif undo_open is None and canvas_blank is False:
                # 标定要在刻度带画测试线；没有撤销标记就清不掉，而画布已有用户
                # 墨迹时更不能冒险——与其留下脏线，不如保持单细笔。
                self._emit(task_id, "task.warning", {
                    "message": "画布已有笔迹且未标记撤销按钮，无法安全清空标定测试线，"
                               "本次回退单细笔绘制（清空画布后重新开始可启用自动变粗）"
                })
            else:
                if panel is not None and panel_open is not True:
                    if panel.unfold(
                        slider_open, self._button_chain(fold_closed, fold_open)
                    ):
                        panel_open = True
                    else:
                        # 展开失败时滑杆可能被画布覆盖，点下去就是墨点：跳过标定。
                        self._emit(task_id, "task.warning", {
                            "message": "工具面板无法展开，跳过笔宽标定（本次单细笔绘制）"
                        })
                if panel is None or panel_open is True:
                    params = self._run_pen_calibration(
                        task_id, params, mouse, target, client, image_size,
                        new_rect, slider_open, undo_open, handle,
                        cursor=cursor, total=total,
                    )
                    if params is None:  # 标定被停止，已落 STOPPED
                        return

        # 宽度表存在即视为自动变粗生效（哪怕只有 1 档）：面板必须收起，否则
        # 会遮住画布下部。多档才涉及换笔与分桶。
        has_width_table = params.width_table is not None
        multi_width = has_width_table and len(params.width_table or ()) >= 2

        # 开画前收起工具面板（滑杆不再需要常开）。面板位于画布下方的工具条，
        # 收起后画布可能扩大并盖住滑杆区域（见 panel_control）：必须按收起态
        # 重取矩形并校验漂移，超限即停，避免映射基准错位让整幅笔迹偏移。
        panel_active = panel is not None and has_width_table
        mapping_rect = new_rect
        if panel_active:
            fold_buttons = self._button_chain(fold_open, fold_closed)
            unfold_buttons = self._button_chain(fold_closed, fold_open)
            if slider_open is not None and panel.fold(slider_open, fold_buttons):
                panel_open = False
            handle.settled_cursor = None
            if panel_open is False:
                collapsed_rect = self._current_canvas_rect(target)
                if collapsed_rect is None:
                    self._emit(task_id, "task.warning", {
                        "message": "无法确认收起态画布矩形，落笔映射沿用任务开局矩形"
                    })
                else:
                    if self._canvas_size_drifted(collapsed_rect, new_rect):
                        raise TaskError(
                            409,
                            "E_CANVAS_DRIFT",
                            "工具面板开合改变了画布矩形（开局 x,y,w,h="
                            f"{new_rect.x},{new_rect.y},{new_rect.w},{new_rect.h} / 收起 "
                            f"{collapsed_rect.x},{collapsed_rect.y},"
                            f"{collapsed_rect.w},{collapsed_rect.h}），"
                            "本任务已停止以免笔迹错位；请在面板收起状态重新校准",
                        )
                    mapping_rect = collapsed_rect

        # 首笔前强制画笔校验：绝不在橡皮态下笔。旧校准无法判定，降级放行并提示。
        if legacy_mode:
            self._emit(task_id, "task.warning", {
                "message": "旧校准：无法校验画笔态（请重新校准以启用画笔/橡皮识别）"
            })
        else:
            self._ensure_brush_before_draw(
                task_id, tools_open, tools_collapsed, panel, slider_open,
                fold_open, fold_closed, panel_open, mouse, target, image_size, client,
                canvas=new_rect,
            )

        flat = self._build_draw_sequence(paths, params)
        bucket_count = len({bucket for bucket, _edge, _path in flat})
        if bucket_count > 1:
            self._emit(task_id, "task.warning", {
                "message": f"按容差 {params.width_tolerance:.2f} 分 {bucket_count} 档笔宽绘制"
            })

        handle.guard_active.set()
        self._transition(task_id, "DRAWING")
        last_pct = -1
        point_delay_s = max(0.0, params.point_delay_ms) / 1000
        stroke_delay_s = max(0.0, params.stroke_delay_ms) / 1000
        active_bucket: int | None = None
        # 画布差分守护只对 mirror 目标启用：PC 目标上用户鼠标笔画同样是注入
        # 事件，画面差分与注入墨迹不可区分，强开只会把正常绘制误停。
        canvas_guard_active = params.canvas_guard and target.kind == "mirror"
        guard_scale_x = mapping_rect.w / max(1, calibration.rect_w)
        guard_scale_y = mapping_rect.h / max(1, calibration.rect_h)
        guard_region = (mapping_rect.x, mapping_rect.y, mapping_rect.w, mapping_rect.h)

        for index in range(cursor, total):
            if handle.stop_event.is_set():
                self._finish_stopped(task_id, index, total, started, handle)
                return
            if not target.alive():
                raise TaskError(409, "E_WINDOW_LOST", "绘制期间目标窗口丢失")

            bucket, target_frac, canvas_path = flat[index]
            if (
                multi_width
                and slider_open is not None
                and bucket != active_bucket
            ):
                # 换笔：展开 → 校验画笔态 → 点滑杆到桶下缘 → 折叠。置空静置
                # 光标，避免把滑杆点击误判成用户接管。展开失败则降级保持当前
                # 笔宽继续画，绝不盲点。
                handle.settled_cursor = None
                switched = True
                if panel is not None and panel_open is not True:
                    switched = panel.unfold(slider_open, unfold_buttons)
                    panel_open = True if switched else False
                    if not switched:
                        self._emit(task_id, "task.warning", {
                            "message": f"工具面板无法展开，第 {bucket + 1} 档沿用当前笔宽"
                        })
                try:
                    if switched and not handle.stop_event.is_set():
                        # 面板展开后画布可能被工具条撑大（折叠态盖住滑杆区域），
                        # 必须按当前帧重新取画布做守卫，否则滑杆/画笔坐标会被
                        # 折叠态的旧矩形判成「落在画布内」而全部拒点。
                        guard_canvas = self._current_canvas_rect(target) or new_rect
                        # 展开态工具格可见：换笔前再确认一次画笔态，非画笔则先切回。
                        self._ensure_brush(
                            task_id, tools_open, mouse, target, image_size, client,
                            canvas=guard_canvas,
                        )
                        # 点位取桶下缘（target_frac 即 bucket*tol），保证实际不粗于计划。
                        sx = slider_open["x"] + slider_open["w"] * target_frac
                        sy = slider_open["y"] + slider_open["h"] / 2.0
                        if self._click_image_point(
                            mouse, target, (sx, sy), image_size, client,
                            canvas=guard_canvas,
                        ):
                            if handle.stop_event.wait(0.25):
                                self._finish_stopped(task_id, index, total, started, handle)
                                return
                            self._emit(task_id, "task.warning", {
                                "message": f"已切换第 {bucket + 1} 档笔宽（目标行程 {target_frac:.2f}）"
                            })
                        else:
                            self._emit(task_id, "task.warning", {
                                "message": f"滑杆坐标落在画布内，第 {bucket + 1} 档沿用当前笔宽"
                            })
                        active_bucket = bucket
                finally:
                    # 任何异常（含 E_NOT_BRUSH）都先把面板收起，避免遮挡画布下部。
                    # 停止意图优先于收尾：已停笔则不再绘制，折叠与映射漂移校验毫无
                    # 意义，且它们在慢环境里各要一次截图，会把停笔拖成秒级。
                    if (panel_active and slider_open is not None
                            and not handle.stop_event.is_set()):
                        if panel.fold(slider_open, fold_buttons):  # type: ignore[union-attr]
                            panel_open = False
                        else:
                            # 折叠失败：面板仍展开，画布可能被撑小，收起态基准
                            # 已不可信。已有异常在传播时只告警，不掩盖原错误。
                            self._guard_mapping_drift(
                                task_id, target, mapping_rect,
                                primary_exc=sys.exc_info()[0] is not None,
                            )
                    handle.settled_cursor = None
            if handle.stop_event.is_set():
                # 换宽过程中被停止（ESC/最小化/接管）：立即收尾，不再补画当前笔。
                self._finish_stopped(task_id, index, total, started, handle)
                return
            screen_path = [
                image_point_to_screen(
                    mapping_rect.x + x * mapping_rect.w / max(1, calibration.rect_w),
                    mapping_rect.y + y * mapping_rect.h / max(1, calibration.rect_h),
                    image_w,
                    image_h,
                    client,
                )
                for x, y in canvas_path
            ]
            handle.settled_cursor = None
            # 差分守护的 P 帧：换宽/换笔的收尾（面板折回）已完成，此刻起画布上
            # 出现任何注入笔迹邻域外的新墨都是预期外（手指误触）。
            guard_before = target.capture_image() if canvas_guard_active else None
            if len(screen_path) == 1 or path_length(screen_path) < params.short_stroke_threshold_px:
                hold = max(0.010, params.click_hold_ms / 1000)
                if params.short_stroke_protect:
                    hold = max(hold, 0.022)
                handle.stroking.set()
                mouse.click(*screen_path[0], hold=hold)  # type: ignore[attr-defined]
                # 先落静置点再解除笔画标志：反过来会留出「旧静置点 vs 新光标」的误判窗口。
                handle.settled_cursor = (round(screen_path[0][0]), round(screen_path[0][1]))
                handle.stroking.clear()
            else:
                handle.stroking.set()
                completed = mouse.drag_polyline(  # type: ignore[attr-defined]
                    screen_path,
                    event_step_px=params.event_step_px,
                    point_delay_s=point_delay_s,
                    should_stop=handle.stop_event.is_set,
                    no_coalesce=params.no_coalesce,
                    short_stroke_protect=params.short_stroke_protect,
                )
                if completed:
                    handle.settled_cursor = (round(screen_path[-1][0]), round(screen_path[-1][1]))
                    handle.stroking.clear()
                else:
                    self._finish_stopped(task_id, index, total, started, handle)
                    return

            done = index + 1
            self._store.update_task(task_id, cursor_path=done)
            if canvas_guard_active and guard_before is not None:
                stop_reason = self._canvas_diff_reason(
                    guard_before, target, canvas_path, guard_region,
                    guard_scale_x, guard_scale_y,
                )
                if stop_reason is not None:
                    handle.request_stop(stop_reason)
                    self._finish_stopped(task_id, done, total, started, handle)
                    return
            pct = int(done * 100 / max(1, total))
            if total <= 50 or (pct != last_pct and (pct % 2 == 0 or pct == 100)):
                last_pct = pct
                self._emit(task_id, "task.progress", {
                    "donePaths": done,
                    "totalPaths": total,
                    "percent": pct,
                    "elapsedMs": int((time.monotonic() - started) * 1000),
                })
            if stroke_delay_s and handle.stop_event.wait(stroke_delay_s):
                self._finish_stopped(task_id, done, total, started, handle)
                return

        self._transition(task_id, "DONE", finished_at=utc_now())
        self._emit(task_id, "task.done", {
            "status": "DONE", "donePaths": total, "totalPaths": total,
            "elapsedMs": int((time.monotonic() - started) * 1000),
        })

    def _canvas_diff_reason(
        self,
        before: object,
        target: DrawingTarget,
        canvas_path: list[tuple[float, float]],
        region: tuple[int, int, int, int],
        scale_x: float,
        scale_y: float,
    ) -> str | None:
        """笔迹结束点的画布差分：返回停止原因，无异常返回 None。

        检测点选在每条笔迹的 Q 帧：手指墨迹无论落在注入时间窗内还是窗外都会
        出现在这里，任何误触最迟在下一次落笔前被拦下。截图失败静默放行——
        差分是增强路径，不得因瞬时截图抖动中断绘制。
        """
        after = target.capture_image()
        if after is None:
            return None
        stroke_image = [
            (region[0] + x * scale_x, region[1] + y * scale_y) for x, y in canvas_path
        ]
        stray, screen_changed = detect_stray_ink(
            before, after, stroke_image, region,
            radius_px=STRAY_RADIUS_PX, min_px=STRAY_MIN_PIXELS,
        )
        if screen_changed:
            return "screen_changed"
        return "canvas_touched" if stray else None

    def _finish_stopped(
        self, task_id: str, done: int, total: int, started: float, handle: _RunHandle
    ) -> None:
        reason = handle.reason or "user"
        self._transition(task_id, "STOPPED", stop_reason=reason, finished_at=utc_now())
        self._emit(task_id, "task.done", {
            "status": "STOPPED", "stopReason": reason,
            "donePaths": done, "totalPaths": total,
            "elapsedMs": int((time.monotonic() - started) * 1000),
        })

    def _finish_failed(
        self, task_id: str, code: str, message: str, done: int, total: int, started: float
    ) -> None:
        self._transition(task_id, "FAILED", error_code=code, finished_at=utc_now())
        self._emit(task_id, "task.done", {
            "status": "FAILED", "errorCode": code, "message": message,
            "donePaths": done, "totalPaths": total,
            "elapsedMs": int((time.monotonic() - started) * 1000),
        })

    def _cursor(self, task_id: str) -> int:
        record = self._store.get_task(task_id)
        return record.cursor_path if record else 0

    # ---------- 自动变粗 ----------

    @staticmethod
    def _probe_band_rect(canvas: Rect) -> tuple[int, int, int, int]:
        """工具栏探测自用的画布顶部条带（探测笔迹落点与观测区域）。

        只要求落在画布内且足够容纳探测线；标定基线已改由 PenCalibrator 按其
        真实 band 计算，不再复用这里的几何推导。
        """
        margin = PenCalibrator.TEST_MARGIN
        x0 = int(canvas.x) + margin
        y0 = int(canvas.y) + margin
        width = max(1, min(PenCalibrator.TEST_LINE_LEN + 10, int(canvas.w) - margin))
        rows = CALIBRATION_PROBE_TIERS
        height = max(1, min(int(canvas.h) - margin, 16 + (rows - 1) * BAND_ROW_STEP_PX + 32))
        return (x0, y0, width, height)

    def _run_pen_calibration(
        self,
        task_id: str,
        params: ExecParams,
        mouse: object,
        target: DrawingTarget,
        client: object,
        image_size: tuple[int, int],
        canvas: Rect,
        slider: dict | None,
        undo: dict | None,
        handle: _RunHandle,
        *,
        cursor: int,
        total: int,
    ) -> ExecParams | None:
        """绘制前笔宽标定；返回带 width_table 的新 params，被停止返回 None。"""
        if slider is None:
            raise TaskError(
                422, "E_SLIDER_RECT",
                "自动变粗需要校准信息：请先在校准页框选笔粗细滑杆轨道",
            )
        image_w, image_h = image_size
        calibrator = PenCalibrator(
            probe=target,
            mouse=mouse,
            to_screen=lambda x, y: image_point_to_screen(x, y, image_w, image_h, client),
        )
        self._emit(task_id, "task.warning", {"message": "开始笔宽自动标定（约需 1 分钟）"})
        try:
            result = calibrator.run(
                slider_track=slider,
                undo_point=undo,
                tiers=CALIBRATION_PROBE_TIERS,
                canvas=(canvas.x, canvas.y, canvas.w, canvas.h),
                on_progress=lambda msg: self._emit(task_id, "task.warning", {"message": msg}),
                should_stop=handle.stop_event.is_set,
            )
        except CalibrationError as exc:
            if exc.code == "E_STOPPED":
                # 标定发生在绘制之前：进度必须如实反映已画笔数与总数，否则前端
                # totalPaths 变 0、续画入口错乱。
                self._finish_stopped(task_id, cursor, total, time.monotonic(), handle)
                return None
            raise TaskError(422, exc.code, exc.message) from exc

        if result.width_spread < PenCalibrator.MIN_SPREAD_RATIO:
            self._emit(task_id, "task.warning", {
                "message": "该画板各粗细档笔宽几乎一致，已回退单细笔绘制"
            })
        new_params = replace(params, width_table=result.table.tiers)
        self._store.update_task(task_id, exec_params=new_params.as_dict())
        return new_params

    def _build_draw_sequence(
        self,
        paths: list[list[tuple[float, float]]],
        params: ExecParams,
    ) -> list[tuple[int, float, list[tuple[float, float]]]]:
        """无宽度表或仅 1 档时平铺为单桶；否则按容差 + 局部密度分桶（先粗后细）。

        返回 [(桶号, 桶下缘行程, 笔迹)]，桶号越大越粗。续画会基于同一份
        width_table 与容差重算，结果必须逐位一致（有锁定测试）。纯函数，
        不在此发消息，分档提示由调用点在拿到结果后发出。
        """
        if params.width_table is None or len(params.width_table) < 2:
            return [(0, 0.0, p) for p in paths]
        buckets = width_buckets(paths, params.width_tolerance)
        return [
            (bucket, edge, path)
            for bucket, edge, group in buckets
            for path in group
        ]

    @staticmethod
    def _legacy_tier_order(raw: dict) -> bool:
        """判断是否为旧版按档位排序创建的记录（widthTiers 语义）。

        旧版把 width_table 的每一档再按密度重排；新版按容差重新分桶，顺序可能
        不同。只有多档宽度表才涉及重排，单档/无表仍是原始笔序，可安全续画。
        """
        table = raw.get("widthTable")
        return (
            "widthTiers" in raw
            and "widthTolerance" not in raw
            and bool(table)
            and len(table) >= 2
        )

    @staticmethod
    def _shift_rect(rect: dict | None, dx: float, dy: float) -> dict | None:
        if rect is None:
            return None
        return {"x": rect["x"] + dx, "y": rect["y"] + dy, "w": rect["w"], "h": rect["h"]}

    @staticmethod
    def _shift_point(point, dx: float, dy: float) -> dict | None:
        """点统一成 {"x","y"}（标定器/面板控制器都吃 dict）。"""
        if point is None:
            return None
        if isinstance(point, dict):
            return {"x": float(point["x"]) + dx, "y": float(point["y"]) + dy}
        return {"x": float(point[0]) + dx, "y": float(point[1]) + dy}

    @staticmethod
    def _point_dict(point) -> dict | None:
        """tuple / dict 点 → {"x","y"} dict。"""
        if point is None:
            return None
        if isinstance(point, dict):
            return {"x": float(point["x"]), "y": float(point["y"])}
        return {"x": float(point[0]), "y": float(point[1])}

    @staticmethod
    def _button_chain(*points: dict | None) -> list[dict]:
        """按钮候选链：去 None、按坐标去重，保持方向优先级顺序。"""
        chain: list[dict] = []
        seen: set[tuple[float, float]] = set()
        for point in points:
            if point is None:
                continue
            key = (float(point["x"]), float(point["y"]))
            if key in seen:
                continue
            seen.add(key)
            chain.append(point)
        return chain

    def _tool_bitmap(self, raw: dict | None, dx: float, dy: float) -> ToolBitmap | None:
        """持久化的工具位图按画布平移，还原为运行时 ToolBitmap。

        入参可能来自 JSON（点被序列化成 list/dict），这里统一成 tuple。
        """
        if not raw:
            return None
        data = dict(raw)

        def shifted(name: str) -> tuple[float, float] | None:
            point = data.get(name)
            if point is None:
                return None
            if isinstance(point, dict):
                return (float(point["x"]) + dx, float(point["y"]) + dy)
            return (float(point[0]) + dx, float(point[1]) + dy)

        slider_raw = data.get("slider")
        slider = None
        if slider_raw:
            slider = {
                "x": float(slider_raw["x"]) + dx,
                "y": float(slider_raw["y"]) + dy,
                "w": float(slider_raw["w"]),
                "h": float(slider_raw["h"]),
            }
        return ToolBitmap(
            brush=shifted("brush"),
            eraser=shifted("eraser"),
            slider=slider,
            undo=shifted("undo"),
            fold=shifted("fold"),
            brush_selected_lum=data.get("brush_selected_lum"),
            brush_idle_lum=data.get("brush_idle_lum"),
        )

    def _make_panel(
        self,
        params: ExecParams,
        target: DrawingTarget,
        mouse: object,
        image_size: tuple[int, int],
        client: object,
        slider: dict | None,
        fold: dict | None,
        stop_event: threading.Event,
    ) -> PanelController | None:
        """面板开合控制器：仅自动变粗 + 滑杆与折叠按钮都有坐标时启用。"""
        if not (params.auto_width and slider is not None and fold is not None):
            return None
        image_w, image_h = image_size
        return PanelController(
            probe=target,
            mouse=mouse,
            to_screen=lambda x, y: image_point_to_screen(x, y, image_w, image_h, client),
            stopped=stop_event.is_set,
            # 面板开合的每次 settle 都以「停止即醒」等待实现：慢环境里
            # 0.45s×多轮的开合等待会把用户 ESC/最小化的停笔拖成秒级。
            sleep=stop_event.wait,
        )

    def _click_image_point(
        self,
        mouse: object,
        target: DrawingTarget,
        point: tuple[float, float],
        image_size: tuple[int, int],
        client: object,
        canvas: Rect | None = None,
    ) -> bool:
        """图片坐标转屏幕后点击；点击前确保目标在前台。

        给了 ``canvas`` 时，落在画布内的点一律拒绝点击（防止工具/滑杆坐标错位
        时在用户画作上落墨）。返回是否真的点击。
        """
        if canvas is not None and _point_in_rect(point, canvas):
            return False
        if not target.foreground_matches():
            target.activate()
        image_w, image_h = image_size
        px, py = image_point_to_screen(point[0], point[1], image_w, image_h, client)
        mouse.click(px, py)  # type: ignore[attr-defined]
        return True

    def _ensure_brush(
        self,
        task_id: str,
        tools: ToolBitmap | None,
        mouse: object,
        target: DrawingTarget,
        image_size: tuple[int, int],
        client: object,
        canvas: Rect | None = None,
    ) -> None:
        """按画笔格亮度指纹确认当前为画笔；非画笔则点一次画笔按钮复检。

        无完整指纹（brush 坐标缺失，或 selected/idle 亮度只到一侧）时无从判定，
        降级放行并提示——部分指纹只能用于定位坐标，不得代入亮度阈值。
        """
        if tools is None or tools.brush is None:
            self._emit(task_id, "task.warning", {
                "message": "工具位图缺少画笔坐标，本次跳过画笔校验"
            })
            return
        if tools.brush_selected_lum is None or tools.brush_idle_lum is None:
            self._emit(task_id, "task.warning", {
                "message": "画笔选中指纹不完整，本次跳过画笔校验"
            })
            return
        if self._brush_selected(tools, target):
            return
        if not self._click_image_point(
            mouse, target, tools.brush, image_size, client, canvas=canvas,
        ):
            self._emit(task_id, "task.warning", {
                "message": "画笔按钮坐标落在画布内，已拒绝点击"
            })
            raise TaskError(
                409, "E_NOT_BRUSH",
                "画笔按钮坐标落在画布内，无法切换工具；已停止以免擦除画面",
            )
        time.sleep(0.25)
        if not self._brush_selected(tools, target):
            raise TaskError(
                409, "E_NOT_BRUSH",
                "工具条画笔格未处于选中态，可能已被橡皮/其他工具占用；已停止以免擦除画面",
            )
        self._emit(task_id, "task.warning", {"message": "检测到非画笔工具，已自动切回画笔"})

    def _brush_selected(self, tools: ToolBitmap, target: DrawingTarget) -> bool:
        """画笔格中心亮度 >= (选中+未选中)/2 即视为选中。

        取样盒与 ToolbarProber._cell_lum 一致（±4px 均值），否则探测得到的
        指纹在绘制期不可比。截图失败时按「已选中」放行，避免误伤绘制。
        """
        if tools.brush_selected_lum is None or tools.brush_idle_lum is None:
            return True
        if tools.brush is None:
            return True
        image = target.capture_image()
        if image is None:
            return True
        gray = image[..., 0] if image.ndim == 3 else image
        x, y = int(tools.brush[0]), int(tools.brush[1])
        crop = gray[max(0, y - 4) : y + 5, max(0, x - 4) : x + 5]
        if crop.size == 0:
            return True
        lum = float(crop.mean())
        return lum >= (tools.brush_selected_lum + tools.brush_idle_lum) / 2.0

    def _ensure_brush_before_draw(
        self,
        task_id: str,
        tools_open: ToolBitmap | None,
        tools_collapsed: ToolBitmap | None,
        panel: PanelController | None,
        slider: dict | None,
        fold_open: dict | None,
        fold_closed: dict | None,
        panel_open: bool | None,
        mouse: object,
        target: DrawingTarget,
        image_size: tuple[int, int],
        client: object,
        canvas: Rect | None = None,
    ) -> None:
        """首笔前画笔态校验；折叠态有折叠位图就用，否则展开后按展开位图判定。

        折叠态/未知态拿不到确定的展开态时直接跳过校验（只警告、不点击）：
        用展开态坐标判折叠态界面会在白画布处「假通过」，橡皮 bug 会复发。
        """
        if (
            panel_open is False
            and tools_collapsed is not None
            and tools_collapsed.brush is not None
        ):
            self._ensure_brush(
                task_id, tools_collapsed, mouse, target, image_size, client, canvas=canvas,
            )
            return

        state = panel_open
        if state is None and panel is not None and slider is not None:
            state = panel.is_open(slider)
        unfolded = False
        if state is not True:
            if panel is not None and slider is not None:
                unfolded = panel.unfold(
                    slider, self._button_chain(fold_closed, fold_open)
                )
            if not unfolded:
                self._emit(task_id, "task.warning", {
                    "message": "折叠态无画笔位图，本次跳过画笔校验"
                })
                return
        try:
            if unfolded:
                guard_canvas = self._current_canvas_rect(target) or canvas
            else:
                guard_canvas = canvas
            self._ensure_brush(
                task_id, tools_open, mouse, target, image_size, client,
                canvas=guard_canvas,
            )
        finally:
            if unfolded:
                panel.fold(  # type: ignore[union-attr]
                    slider, self._button_chain(fold_open, fold_closed)
                )

    def _current_canvas_rect(self, target: DrawingTarget) -> Rect | None:
        """按当前截图重取画布矩形。

        面板开合会改变画布区域（折叠时更大、可能盖住工具条），跨态点击的
        守卫必须用「点击那一刻」的画布，而不是展开前那张图推出来的矩形。
        """
        image = target.capture_image()
        if image is None:
            return None
        found = detect_canvas_rect(image)
        if found is None:
            return None
        left, top, right, bottom = found
        return Rect(left, top, right - left, bottom - top)

    @staticmethod
    def _canvas_size_drifted(current: Rect, baseline: Rect) -> bool:
        """收起态画布与开局矩形的偏差是否超过 max(8px, 3% 对应边长)。

        收起面板可能让画布区域变大（甚至盖住工具条）；偏差过大说明映射基准
        不可信，继续落笔会让整幅笔迹整体错位，必须停止而不是静默沿用。
        """
        checks = (
            (current.x, baseline.x, baseline.w),
            (current.y, baseline.y, baseline.h),
            (current.w, baseline.w, baseline.w),
            (current.h, baseline.h, baseline.h),
        )
        return any(
            abs(float(a) - float(b)) > max(8.0, 0.03 * float(side))
            for a, b, side in checks
        )

    def _guard_mapping_drift(
        self,
        task_id: str,
        target: DrawingTarget,
        mapping_rect: Rect,
        *,
        primary_exc: bool,
    ) -> None:
        """换宽后折叠失败时，确认画布仍与收起态映射基准一致。

        面板没收起说明画布可能仍被工具条撑小（或已重新变大），继续按收起态
        ``mapping_rect`` 映射会静默错位。此刻若已有异常在传播，只能告警而不能
        抛出新异常掩盖原始错误。
        """
        rect = self._current_canvas_rect(target)
        if rect is None:
            self._emit(task_id, "task.warning", {
                "message": "无法确认当前画布矩形，按收起态基准继续绘制"
            })
            return
        if not self._canvas_size_drifted(rect, mapping_rect):
            return
        message = (
            "工具面板未能收起且画布矩形已变化（收起基准 x,y,w,h="
            f"{mapping_rect.x},{mapping_rect.y},{mapping_rect.w},{mapping_rect.h} / 当前 "
            f"{rect.x},{rect.y},{rect.w},{rect.h}），"
            "已停止以免笔迹错位；请重新校准"
        )
        if primary_exc:
            self._emit(task_id, "task.warning", {"message": message})
            return
        raise TaskError(409, "E_CANVAS_DRIFT", message)

    def _canvas_is_blank(
        self, target: DrawingTarget, canvas: Rect, calibration: CanvasProfile
    ) -> bool:
        """探测前空白门禁：画布已有墨迹时不得在下方候选区盲点。

        截图失败按空白处理并继续（无法判定时不因门禁额外阻断）；判定走统一
        空白判据（clear-and-guide-design §3：自适应背景 + 空白基线 + 绝对封顶），
        水印/纹理背景不再把干净画布误判成有墨。
        """
        image = target.capture_image()
        if image is None:
            return True
        return self._is_canvas_blank(
            image, (canvas.x, canvas.y, canvas.w, canvas.h), calibration
        )

    def _is_canvas_blank(
        self, image, region: tuple[int, int, int, int], calibration: CanvasProfile
    ) -> bool:
        """统一空白判定 + 基线自学习（clear-and-guide-design §3.3/§3.4）。

        基线失效（整幅翻转）先作废；判空白且尚无基线时快照一张。基线读写
        失败都不影响判定结论——它是增强层，不是正确性依赖。
        """
        baseline = calibration.blank_baseline
        try:
            if canvas_blankness.baseline_is_stale(image, region, baseline):
                self._persist_blank_baseline(calibration, None)
                baseline = None
            blank = canvas_blankness.is_blank(image, region, baseline)
            if blank and baseline is None:
                self._persist_blank_baseline(
                    calibration, canvas_blankness.learn_baseline(image, region)
                )
            return blank
        except Exception:  # 掩码编解码等任何异常退回无基线的 L1 直判
            return canvas_blankness.is_blank(image, region, None)

    def _persist_blank_baseline(
        self, calibration: CanvasProfile, baseline: dict | None
    ) -> None:
        """沿用既有校准记录其余字段、仅替换空白基线，存为新的最新 profile。"""
        try:
            self._store.save_calibration(
                project_id=calibration.project_id,
                hwnd=calibration.hwnd,
                window_title=calibration.window_title,
                capture_method=calibration.capture_method,
                rect_x=calibration.rect_x,
                rect_y=calibration.rect_y,
                rect_w=calibration.rect_w,
                rect_h=calibration.rect_h,
                dpi_scale=calibration.dpi_scale,
                source=calibration.source,
                confidence=calibration.confidence,
                slider=calibration.slider,
                undo=calibration.undo,
                fold=calibration.fold,
                tools_open=calibration.tools_open,
                tools_collapsed=calibration.tools_collapsed,
                device_kind=calibration.device_kind,
                device_serial=calibration.device_serial,
                blank_baseline=baseline,
                snapshot_id=calibration.snapshot_id,
                frame_w=calibration.frame_w,
                frame_h=calibration.frame_h,
            )
        except Exception:  # 基线是增强层：存失败只影响下次判定的精度
            pass

    def _probe_toolbar(
        self,
        task_id: str | None,
        project_id: str,
        calibration: object,
        target: DrawingTarget,
        mouse: object,
        client: object,
        image_size: tuple[int, int],
        canvas: Rect,
        dx: float,
        dy: float,
        fold: dict | None,
        slider: dict | None,
        undo: dict | None,
        handle: _RunHandle,
        *,
        manual: ToolBitmap | None = None,
        rescan_collapsed: bool = False,
    ) -> ToolBitmap | None:
        """旧校准的行为探测，成功后持久化为一条带双态位图的新校准记录。

        失败只警告（回退旧单套坐标），不阻断绘制。``task_id=None`` 用于清空
        画布等无任务流程：不推 SSE，仅返回结果。

        ``manual`` 为已知位图（如复制的校准的 tools_open）时，作为人工兜底传给
        探测，已知控件不再重扫；``rescan_collapsed`` 为真时额外「收起面板→重扫」
        产出收起态位图（含「展开」钮），供执行期展开面板用。
        """

        def warn(payload: dict) -> None:
            if task_id is not None:
                self._emit(task_id, "task.warning", payload)

        def on_step(event: dict) -> None:
            # 执行链只报控件终态；盲扫进度专供校准页，避免刷爆任务 warning。
            if event.get("state") == STATE_RUNNING:
                return
            warn(step_to_warning(event))

        # 旧校准的既有坐标作为人工兜底传入；已被识别项不再重复探测。显式给了
        # manual（如复制的校准的 tools_open）则整体沿用，不再从单套坐标拼装。
        if manual is None:
            manual = ToolBitmap(
                brush=None,
                eraser=None,
                slider=dict(slider) if slider is not None else None,
                undo=(undo["x"], undo["y"]) if undo is not None else None,
                fold=(fold["x"], fold["y"]) if fold is not None else None,
                brush_selected_lum=None,
                brush_idle_lum=None,
            )
        image_w, image_h = image_size
        prober = ToolbarProber(
            probe=target,
            mouse=mouse,
            to_screen=lambda x, y: image_point_to_screen(x, y, image_w, image_h, client),
        )
        try:
            tools = prober.run(
                canvas=(canvas.x, canvas.y, canvas.w, canvas.h),
                band=self._probe_band_rect(canvas),
                open_bitmap=manual,
                on_step=on_step,
                should_stop=handle.stop_event.is_set,
                rescan_collapsed=rescan_collapsed,
            )
        except ProbeError as exc:
            if exc.code == "E_STOPPED":
                return None
            warn({
                "message": f"工具条自动探测失败（{exc.code}），沿用旧校准坐标",
                "code": "probe_failed",
            })
            return None
        except Exception as exc:  # 探测是增强路径，任何异常都不得中断绘制
            warn({
                "message": f"工具条自动探测异常（{exc.__class__.__name__}），沿用旧校准坐标",
                "code": "probe_failed",
            })
            return None

        try:
            self._persist_probed_calibration(project_id, calibration, tools, dx, dy)
        except Exception as exc:  # 持久化失败不影响本次绘制
            warn({
                "message": f"探测结果保存失败（{exc.__class__.__name__}），本次沿用内存坐标"
            })
        return tools

    def _persist_probed_calibration(
        self,
        project_id: str,
        calibration: object,
        tools: ToolBitmap,
        dx: float,
        dy: float,
        *,
        rect: Rect | None = None,
        window_title: str | None = None,
        capture_method: str | None = None,
        snapshot_id: str | None = None,
        frame_w: int = 0,
        frame_h: int = 0,
    ) -> None:
        """沿用既有校准记录其余字段、仅替换双态位图，存为新的最新 profile。

        ``tools`` 来自本次截图（new_rect 帧），而 profile 的 rect_* 仍是旧帧；
        写入前必须减掉 (dx, dy) 回到旧帧，否则下次读取 _tool_bitmap(dx, dy)
        会再平移一次，造成双重平移。一键自动校准显式传入本次识别的 ``rect``
        等新锚点，此时坐标与 rect 同帧（dx=dy=0），无需回移。
        """
        raw = asdict(tools)
        # collapsed 是伴生的收起态位图（frozen dataclass 嵌套），直接 asdict 会
        # 把整棵嵌套树写进 tools_open_json；单独剥离后平移入库 tools_collapsed。
        collapsed_raw = raw.pop("collapsed", None)
        stored = self._tool_bitmap(raw, -dx, -dy)
        assert stored is not None
        stored_collapsed = self._shifted_bitmap(collapsed_raw, -dx, -dy)
        legacy_slider = getattr(calibration, "slider", None)
        legacy_undo = getattr(calibration, "undo", None)
        legacy_fold = getattr(calibration, "fold", None)
        if rect is not None:
            # 一键自动校准：旧帧遗留的 slider/undo/fold 已无意义，直接以本次
            # 探测结果覆盖（与 tools_open 同帧），保持单套坐标字段自洽。
            legacy_slider = stored.slider
            legacy_undo = self._point_dict(stored.undo)
            legacy_fold = self._point_dict(stored.fold)
        self._store.save_calibration(
            project_id=project_id,
            hwnd=calibration.hwnd,  # type: ignore[attr-defined]
            window_title=(
                window_title
                if window_title is not None
                else calibration.window_title  # type: ignore[attr-defined]
            ),
            capture_method=(
                capture_method
                if capture_method is not None
                else calibration.capture_method  # type: ignore[attr-defined]
            ),
            rect_x=rect.x if rect is not None else calibration.rect_x,  # type: ignore[attr-defined]
            rect_y=rect.y if rect is not None else calibration.rect_y,  # type: ignore[attr-defined]
            rect_w=rect.w if rect is not None else calibration.rect_w,  # type: ignore[attr-defined]
            rect_h=rect.h if rect is not None else calibration.rect_h,  # type: ignore[attr-defined]
            dpi_scale=calibration.dpi_scale,  # type: ignore[attr-defined]
            source="probe",
            confidence=calibration.confidence,  # type: ignore[attr-defined]
            slider=legacy_slider,
            undo=legacy_undo,
            fold=legacy_fold,
            tools_open=asdict(stored),
            # 本次探测带回收起态产物就用新的；执行链探测不重扫收起态，继承上一份
            # （多为校准页一键自动校准的产出），绝不能用 None 把已有成果抹掉。
            tools_collapsed=(
                stored_collapsed
                if stored_collapsed is not None
                else getattr(calibration, "tools_collapsed", None)
            ),
            device_kind=calibration.device_kind,  # type: ignore[attr-defined]
            device_serial=calibration.device_serial,  # type: ignore[attr-defined]
            blank_baseline=getattr(calibration, "blank_baseline", None),
            snapshot_id=(
                snapshot_id
                if snapshot_id is not None
                else getattr(calibration, "snapshot_id", None)
            ),
            # 执行链探测沿用旧帧坐标（工具位图已回移到旧帧），frame 未显式
            # 给出时继承旧帧尺寸；一键自动校准显式传本次帧尺寸。
            frame_w=frame_w or getattr(calibration, "frame_w", 0),
            frame_h=frame_h or getattr(calibration, "frame_h", 0),
        )

    def _shifted_bitmap(self, raw: dict | None, dx: float, dy: float) -> dict | None:
        """工具位图 dict（JSON 形态）整体平移；canvas_rect 只平移不改尺寸。"""
        if not raw:
            return None
        shifted = self._tool_bitmap(raw, dx, dy)
        assert shifted is not None
        out = asdict(shifted)
        canvas_rect = raw.get("canvas_rect")
        if canvas_rect:
            out["canvas_rect"] = [
                int(canvas_rect[0]) + dx, int(canvas_rect[1]) + dy,
                int(canvas_rect[2]), int(canvas_rect[3]),
            ]
        return out

    def _start_guard(
        self, task_id: str, mouse: object, target: DrawingTarget, params: ExecParams, handle: _RunHandle
    ) -> None:
        try:
            self._probe.clear_escape_state()
            mouse.clear_escape_state()  # type: ignore[attr-defined]
        except Exception:
            pass

        poll_s = max(0.005, params.guard_poll_ms / 1000)

        def poll() -> None:
            consecutive_errors = 0
            while not handle.guard_event.is_set():
                try:
                    # 守护未激活时也要每轮读一次左键：GetAsyncKeyState 的「自上次
                    # 读取以来被按下」边定位会滞留，标定/清空调试期间的点击不清掉
                    # 的话，落笔后第一轮就会把陈旧边位误判成用户接管。
                    if not handle.guard_active.is_set():
                        self._probe.left_button_pressed()
                    reason = evaluate_signals(
                        handle, self._probe, mouse, target,
                        params.cursor_takeover_px, params.focus_guard,
                    )
                    consecutive_errors = 0
                except Exception:
                    # 信号读取的瞬时故障（截图/窗口 API 抖动）绝不能让守护线程
                    # 退出——线程一死，ESC/接管/焦点全部失灵且毫无感知，表现为
                    # 「程序怎么都不停」。连续失败到阈值才放弃，并发 warning 告知。
                    consecutive_errors += 1
                    if consecutive_errors >= GUARD_ERROR_LIMIT:
                        self._emit(task_id, "task.warning", {"message": (
                            "输入主权守护连续读取失败已退出：鼠标接管将无法自动检出，"
                            "需要暂停请按 ESC 或空格"
                        ), "code": "guard_dead"})
                        return
                    time.sleep(poll_s)
                    continue
                if reason is not None:
                    handle.request_stop(reason)
                    return
                time.sleep(poll_s)

        threading.Thread(target=poll, daemon=True).start()

    def _guarded_countdown(
        self, task_id: str, duration_s: float, target: DrawingTarget, handle: _RunHandle
    ) -> str:
        """守护式倒计时：末段要求目标在前台，不满足则冻结，切回稳定后从冻结点继续。

        返回 "done" 或 "stopped"。冻结期间反复推 warning，前端节流展示。
        """
        step_s = 0.05
        final_s = min(self._arm_final_window_s, duration_s)
        # 倒计时期间**不**抢前台：此刻用户需要看着网页里的秒数做准备，过早把
        # 目标窗口提到前台会把网页整个盖住，倒计时就成了黑箱。到末段（下方
        # 稳定窗）再替用户切换，被前台锁拒绝时才退回人工切换提示。
        remaining = max(0.0, duration_s - final_s)
        self._emit_countdown_tick(task_id, remaining)
        last_tick = int(-(-remaining // 1))
        while remaining > 0:
            if handle.stop_event.wait(min(step_s, remaining)):
                return "stopped"
            remaining -= step_s
            tick = int(-(-remaining // 1))
            if tick != last_tick:
                last_tick = tick
                self._emit_countdown_tick(task_id, remaining)

        # 末段是「前台稳定窗」而非递减计时，前端显示为 0 并提示保持前台。
        self._emit_countdown_tick(task_id, 0.0)
        frozen = False
        last_attempt = 0.0
        stable_left = self._arm_stable_s
        while True:
            if handle.stop_event.is_set():
                return "stopped"
            if target.foreground_matches():
                if frozen:
                    self._emit(task_id, "task.warning", {"message": "目标窗口已回到前台，继续倒计时"})
                    frozen = False
                stable_left -= step_s
                if stable_left <= 0:
                    return "done"
            else:
                stable_left = self._arm_stable_s
                # 先替用户把目标窗口带回前台（含恢复最小化，多数场景可直接成功），
                # 被前台锁拒绝时才退回人工切换提示；节流重试避免刷系统调用。
                now = time.monotonic()
                if now - last_attempt >= 1.0:
                    last_attempt = now
                    if target.activate():
                        continue
                if not frozen:
                    frozen = True
                    self._emit(task_id, "task.warning", {
                        "message": "倒计时已暂停：请把目标窗口切到前台，切回后自动继续"
                    })
            time.sleep(step_s)
