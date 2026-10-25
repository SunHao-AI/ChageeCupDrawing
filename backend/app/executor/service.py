"""执行器服务：任务状态机、守护式倒计时、漂移校验、绘制线程与输入主权守护。

状态只由本服务单向推进（design 6.1），API 层只能发出停止意图。
进度以笔迹条数为单位；每完成一条笔迹持久化 cursor_path，约每 2% 推送一次事件。
绘制期间的守护信号见 executor/guard.py；stop_reason ∈ user/esc/focus_lost/mouse_takeover。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Callable

from app.core.vision.canvas_detect import detect_canvas_rect
from app.device.capture import CaptureError
from app.device.geometry import Rect, image_point_to_screen
from app.device.service import DeviceService
from app.device.target import DrawingTarget, resolve_target
from app.executor.guard import GuardProbe, Win32GuardProbe, evaluate_signals
from app.executor.ink_measure import ink_occupied_fraction
from app.executor.native_mouse import NativeMouse, path_length
from app.executor.panel_control import PanelController
from app.executor.pen_calibration import CalibrationError, PenCalibrator
from app.executor.pen_order import width_buckets
from app.executor.toolbar_probe import ProbeError, ToolBitmap, ToolbarProber
from app.store.models import PlanRecord, TaskRecord, utc_now
from app.store.store import Store

TERMINAL_STATUSES = ("DONE", "FAILED", "STOPPED")

# 倒计时最后这段时间要求目标在前台，不满足则冻结倒计时等待用户切回。
ARM_FINAL_WINDOW_S = 1.0
ARM_STABLE_S = 0.5

# 标定扫描档数：只用于判断滑杆是否真的能调宽（宽度 spread），
# 不再对应绘制档数/桶数（桶数由 width_tolerance 推导）。
CALIBRATION_PROBE_TIERS = 5

# 刻度带工作区：画布顶部条带，标定/探测的测试线都画在这里。基线取样与探测
# 观测必须与标定器用同一块工作区，否则底墨会被算进线宽或误判清空。
BAND_ROW_STEP_PX = 14  # 与 PenCalibrator 的最小行距一致


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


def _rect_iou(a: Rect, b: Rect) -> float:
    ax2, ay2 = a.x + a.w, a.y + a.h
    bx2, by2 = b.x + b.w, b.y + b.h
    ix = max(0, min(ax2, bx2) - max(a.x, b.x))
    iy = max(0, min(ay2, by2) - max(a.y, b.y))
    inter = ix * iy
    union = a.w * a.h + b.w * b.h - inter
    return inter / union if union > 0 else 0.0


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
        self._spawn(
            record,
            plan,
            paths,
            ExecParams.from_dict(record.exec_params),
            mouse,
            target,
            countdown_ms,
            allow_drift,
            cursor=record.cursor_path,
            handle=handle,
        )
        return record, len(paths)

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
        calibration = self._store.latest_calibration(plan.project_id)
        if calibration is None or calibration.rect_w <= 0 or calibration.rect_h <= 0:
            raise TaskError(422, "E_CANVAS_MISSING", "项目尚未完成画布校准")
        return resolve_target(calibration, self._device, self._probe)

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
        return [[(float(x), float(y)) for x, y in path] for path in raw]

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
        self._start_guard(mouse, target, params, handle)
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
        fold_closed = self._point_dict(tools_collapsed.fold if tools_collapsed else None) or fold_open

        panel = self._make_panel(params, target, mouse, image_size, client, slider_open, fold_open)
        # 面板当前态：None 表示未知（旧校准无折叠按钮，坐标直接可用）。
        panel_open: bool | None = None
        if panel is not None and slider_open is not None:
            panel_open = panel.is_open(slider_open)

        # 自动变粗：首绘先标定笔宽表（刻度带测试线→清空画布），续画复用已回写
        # 的宽度表，保证分桶笔序与首绘逐次一致。
        if params.auto_width and params.width_table is None and cursor == 0:
            if slider_open is None:
                self._emit(task_id, "task.warning", {
                    "message": "未标定粗细滑杆，已回退单细笔绘制（可在校准页补标）"
                })
            else:
                if panel is not None and panel_open is not True:
                    if panel.unfold(slider_open, fold_closed):
                        panel_open = True
                    else:
                        # 展开失败时滑杆可能被画布覆盖，点下去就是墨点：跳过标定。
                        self._emit(task_id, "task.warning", {
                            "message": "工具面板无法展开，跳过笔宽标定（本次单细笔绘制）"
                        })
                if panel is None or panel_open is True:
                    # 基线必须在标定开始前、用与标定器相同的刻度带工作区取样并夹到
                    # [0,1]：标定器测宽时会把既有底墨一起算进线宽，基线偏大或偏小
                    # 都会让「测试线是否撤净」的判定失真（Task 5 取样纪律）。
                    baseline = self._band_baseline(target, new_rect)
                    params = self._run_pen_calibration(
                        task_id, params, mouse, target, client, image_size,
                        new_rect, slider_open, undo_open, baseline, handle,
                    )
                    if params is None:  # 标定被停止，已落 STOPPED
                        return

        # 探测接入：旧校准缺双态位图时行为探测一次，成功后把结果持久化成新的
        # 校准记录（沿用其余字段）。失败只警告并沿用旧单套坐标，不阻断绘制。
        if (
            params.auto_width
            and cursor == 0
            and calibration.tools_open is None
            and (slider_open is not None or fold_open is not None)
        ):
            probed = self._probe_toolbar(
                task_id, plan.project_id, calibration, target, mouse, client, image_size,
                new_rect, fold_closed, slider_open, undo_open, handle,
            )
            if handle.stop_event.is_set():
                self._finish_stopped(task_id, cursor, total, started, handle)
                return
            if probed is not None:
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
                if panel is None:
                    panel = self._make_panel(
                        params, target, mouse, image_size, client, slider_open, fold_open
                    )

        multi_width = params.width_table is not None and len(params.width_table) >= 2

        # 开画前收起工具面板（滑杆不再需要常开）。面板位于画布下方的工具条，
        # 收起只腾出可视区域、不改变画布矩形，落笔映射沿用 new_rect。
        panel_active = panel is not None and multi_width
        if panel_active:
            if slider_open is not None and panel.fold(slider_open, fold_open):
                panel_open = False
            handle.settled_cursor = None

        # 首笔前强制画笔校验：绝不在橡皮态下笔。旧校准无法判定，降级放行并提示。
        if legacy_mode:
            self._emit(task_id, "task.warning", {
                "message": "旧校准：无法校验画笔态（请重新校准以启用画笔/橡皮识别）"
            })
        else:
            self._ensure_brush_before_draw(
                task_id, tools_open, tools_collapsed, panel, slider_open,
                fold_open, fold_closed, panel_open, mouse, target, image_size, client,
            )

        flat = self._build_draw_sequence(task_id, paths, params)

        handle.guard_active.set()
        self._transition(task_id, "DRAWING")
        last_pct = -1
        point_delay_s = max(0.0, params.point_delay_ms) / 1000
        stroke_delay_s = max(0.0, params.stroke_delay_ms) / 1000
        active_bucket: int | None = None

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
                    switched = panel.unfold(slider_open, fold_closed)
                    panel_open = True if switched else False
                    if not switched:
                        self._emit(task_id, "task.warning", {
                            "message": f"工具面板无法展开，第 {bucket + 1} 档沿用当前笔宽"
                        })
                if switched:
                    # 展开态工具格可见：换笔前再确认一次画笔态，非画笔则先切回。
                    self._ensure_brush(
                        task_id, tools_open, mouse, target, image_size, client,
                    )
                    # 点位取桶下缘（target_frac 即 bucket*tol），保证实际不粗于计划。
                    sx = slider_open["x"] + slider_open["w"] * target_frac
                    sy = slider_open["y"] + slider_open["h"] / 2.0
                    self._click_image_point(mouse, target, (sx, sy), image_size, client)
                    time.sleep(0.25)
                    active_bucket = bucket
                    self._emit(task_id, "task.warning", {
                        "message": f"已切换第 {bucket + 1} 档笔宽（目标行程 {target_frac:.2f}）"
                    })
                if panel_active and slider_open is not None:
                    if panel.fold(slider_open, fold_open):  # type: ignore[union-attr]
                        panel_open = False
                    handle.settled_cursor = None
            screen_path = [
                image_point_to_screen(
                    new_rect.x + x * new_rect.w / max(1, calibration.rect_w),
                    new_rect.y + y * new_rect.h / max(1, calibration.rect_h),
                    image_w,
                    image_h,
                    client,
                )
                for x, y in canvas_path
            ]
            handle.settled_cursor = None
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
    def _band_rect(canvas: Rect) -> tuple[int, int, int, int]:
        """画布顶部刻度带工作区：标定测试线与探测笔迹都落在这里。

        几何直接沿用 PenCalibrator 的布局常量（左边距 TEST_MARGIN、线长
        TEST_LINE_LEN、首行 y = canvas.y + TEST_MARGIN + 4、最小行距 14px、
        量宽窗上下各留 16/24+8px），使基线取样、探测观测与标定器的判定区域
        是同一块刻度带，而不是整幅画布（整幅画布对几行测试线不敏感）。
        """
        margin = PenCalibrator.TEST_MARGIN
        x0 = int(canvas.x) + margin
        y0 = int(canvas.y) + margin
        width = max(1, min(PenCalibrator.TEST_LINE_LEN + 10, int(canvas.w) - margin))
        rows = CALIBRATION_PROBE_TIERS
        height = max(1, min(int(canvas.h) - margin, 16 + (rows - 1) * BAND_ROW_STEP_PX + 32))
        return (x0, y0, width, height)

    @classmethod
    def _band_baseline(cls, target: DrawingTarget, canvas: Rect) -> float:
        """取刻度带基线占用率。

        取样纪律：必须在标定开始前、用与标定器同一块刻度带工作区取样，并夹到
        [0,1]，否则标定器测到的是「测试线 + 残留底墨」，会把空画布的底墨算进
        线宽；清空判定也会因基线失真而误判。
        """
        image = target.capture_image()
        if image is None:
            return 0.0
        return max(0.0, min(1.0, ink_occupied_fraction(image, cls._band_rect(canvas))))

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
        baseline_occupied: float,
        handle: _RunHandle,
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
                baseline_occupied=baseline_occupied,
                on_progress=lambda msg: self._emit(task_id, "task.warning", {"message": msg}),
                should_stop=handle.stop_event.is_set,
            )
        except CalibrationError as exc:
            if exc.code == "E_STOPPED":
                self._finish_stopped(task_id, 0, 0, time.monotonic(), handle)
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
        task_id: str,
        paths: list[list[tuple[float, float]]],
        params: ExecParams,
    ) -> list[tuple[int, float, list[tuple[float, float]]]]:
        """无宽度表时平铺为单桶；否则按容差 + 局部密度分桶（先粗后细）。

        返回 [(桶号, 桶下缘行程, 笔迹)]，桶号越大越粗。续画会基于同一份
        width_table 与容差重算，结果必须逐位一致（有锁定测试）。
        """
        if params.width_table is None:
            return [(0, 0.0, p) for p in paths]
        buckets = width_buckets(paths, params.width_tolerance)
        self._emit(task_id, "task.warning", {
            "message": f"按容差 {params.width_tolerance:.2f} 分 {len(buckets)} 档笔宽绘制"
        })
        return [
            (bucket, edge, path)
            for bucket, edge, group in buckets
            for path in group
        ]

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
    ) -> PanelController | None:
        """面板开合控制器：仅自动变粗 + 滑杆与折叠按钮都有坐标时启用。"""
        if not (params.auto_width and slider is not None and fold is not None):
            return None
        image_w, image_h = image_size
        return PanelController(
            probe=target,
            mouse=mouse,
            to_screen=lambda x, y: image_point_to_screen(x, y, image_w, image_h, client),
        )

    def _click_image_point(
        self,
        mouse: object,
        target: DrawingTarget,
        point: tuple[float, float],
        image_size: tuple[int, int],
        client: object,
    ) -> None:
        """图片坐标转屏幕后点击；点击前确保目标在前台。"""
        if not target.foreground_matches():
            target.activate()
        image_w, image_h = image_size
        px, py = image_point_to_screen(point[0], point[1], image_w, image_h, client)
        mouse.click(px, py)  # type: ignore[attr-defined]

    def _ensure_brush(
        self,
        task_id: str,
        tools: ToolBitmap | None,
        mouse: object,
        target: DrawingTarget,
        image_size: tuple[int, int],
        client: object,
    ) -> None:
        """按画笔格亮度指纹确认当前为画笔；非画笔则点一次画笔按钮复检。

        无完整指纹（brush 坐标缺失，或 selected/idle 亮度只到一侧）时无从判定，
        降级放行——部分指纹只能用于定位坐标，不得代入亮度阈值。
        """
        if tools is None or tools.brush is None:
            return
        if tools.brush_selected_lum is None or tools.brush_idle_lum is None:
            return
        if self._brush_selected(tools, target):
            return
        self._click_image_point(mouse, target, tools.brush, image_size, client)
        time.sleep(0.15)
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
    ) -> None:
        """首笔前画笔态校验：折叠态优先用折叠位图，否则展开按展开位图判定。"""
        if panel_open is not True and tools_collapsed is not None and tools_collapsed.brush is not None:
            self._ensure_brush(task_id, tools_collapsed, mouse, target, image_size, client)
            return
        state = panel_open
        if state is None and panel is not None and slider is not None:
            state = panel.is_open(slider)
        unfolded = False
        if panel is not None and slider is not None and state is False:
            unfolded = panel.unfold(slider, fold_closed)
        try:
            self._ensure_brush(task_id, tools_open, mouse, target, image_size, client)
        finally:
            if unfolded:
                panel.fold(slider, fold_open)  # type: ignore[union-attr]

    def _probe_toolbar(
        self,
        task_id: str,
        project_id: str,
        calibration: object,
        target: DrawingTarget,
        mouse: object,
        client: object,
        image_size: tuple[int, int],
        canvas: Rect,
        fold: dict | None,
        slider: dict | None,
        undo: dict | None,
        handle: _RunHandle,
    ) -> ToolBitmap | None:
        """旧校准的行为探测，成功后持久化为一条带双态位图的新校准记录。

        失败只警告（回退旧单套坐标），不阻断绘制。
        """
        # 旧校准的既有坐标作为人工兜底传入；已被识别项不再重复探测。
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
                band=self._band_rect(canvas),
                open_bitmap=manual,
                on_progress=lambda msg: self._emit(task_id, "task.warning", {"message": msg}),
                should_stop=handle.stop_event.is_set,
            )
        except ProbeError as exc:
            if exc.code == "E_STOPPED":
                return None
            self._emit(task_id, "task.warning", {
                "message": f"工具条自动探测失败（{exc.code}），沿用旧校准坐标"
            })
            return None
        except Exception as exc:  # 探测是增强路径，任何异常都不得中断绘制
            self._emit(task_id, "task.warning", {
                "message": f"工具条自动探测异常（{exc.__class__.__name__}），沿用旧校准坐标"
            })
            return None

        try:
            self._persist_probed_calibration(project_id, calibration, tools)
        except Exception as exc:  # 持久化失败不影响本次绘制
            self._emit(task_id, "task.warning", {
                "message": f"探测结果保存失败（{exc.__class__.__name__}），本次沿用内存坐标"
            })
        return tools

    def _persist_probed_calibration(
        self, project_id: str, calibration: object, tools: ToolBitmap
    ) -> None:
        """沿用既有校准记录其余字段、仅替换双态位图，存为新的最新 profile。"""
        self._store.save_calibration(
            project_id=project_id,
            hwnd=calibration.hwnd,  # type: ignore[attr-defined]
            window_title=calibration.window_title,  # type: ignore[attr-defined]
            capture_method=calibration.capture_method,  # type: ignore[attr-defined]
            rect_x=calibration.rect_x,  # type: ignore[attr-defined]
            rect_y=calibration.rect_y,  # type: ignore[attr-defined]
            rect_w=calibration.rect_w,  # type: ignore[attr-defined]
            rect_h=calibration.rect_h,  # type: ignore[attr-defined]
            dpi_scale=calibration.dpi_scale,  # type: ignore[attr-defined]
            source=calibration.source,  # type: ignore[attr-defined]
            confidence=calibration.confidence,  # type: ignore[attr-defined]
            slider=calibration.slider,  # type: ignore[attr-defined]
            undo=calibration.undo,  # type: ignore[attr-defined]
            fold=calibration.fold,  # type: ignore[attr-defined]
            tools_open=asdict(tools),
            tools_collapsed=None,
            device_kind=calibration.device_kind,  # type: ignore[attr-defined]
            device_serial=calibration.device_serial,  # type: ignore[attr-defined]
        )

    def _start_guard(
        self, mouse: object, target: DrawingTarget, params: ExecParams, handle: _RunHandle
    ) -> None:
        try:
            self._probe.clear_escape_state()
            mouse.clear_escape_state()  # type: ignore[attr-defined]
        except Exception:
            pass

        poll_s = max(0.005, params.guard_poll_ms / 1000)

        def poll() -> None:
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
                except Exception:
                    return
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
        # 点击开始即替用户把目标窗口带到前台；被前台锁拒绝时退回原有的
        # 「切到前台自动继续」人工流程，不阻断任务。
        if not target.foreground_matches():
            target.activate()
        remaining = max(0.0, duration_s - final_s)
        while remaining > 0:
            if handle.stop_event.wait(min(step_s, remaining)):
                return "stopped"
            remaining -= step_s

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
