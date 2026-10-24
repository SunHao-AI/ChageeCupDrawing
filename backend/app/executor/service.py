"""执行器服务：任务状态机、守护式倒计时、漂移校验、绘制线程与输入主权守护。

状态只由本服务单向推进（design 6.1），API 层只能发出停止意图。
进度以笔迹条数为单位；每完成一条笔迹持久化 cursor_path，约每 2% 推送一次事件。
绘制期间的守护信号见 executor/guard.py；stop_reason ∈ user/esc/focus_lost/mouse_takeover。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from app.core.vision.canvas_detect import detect_canvas_rect
from app.device.capture import CaptureError
from app.device.geometry import Rect, image_point_to_screen
from app.device.service import DeviceService
from app.device.target import DrawingTarget, resolve_target
from app.executor.guard import GuardProbe, Win32GuardProbe, evaluate_signals
from app.executor.native_mouse import NativeMouse, path_length
from app.executor.panel_control import PanelController
from app.executor.pen_calibration import CalibrationError, PenCalibrator
from app.executor.pen_order import tier_groups
from app.store.models import PlanRecord, TaskRecord, utc_now
from app.store.store import Store

TERMINAL_STATUSES = ("DONE", "FAILED", "STOPPED")

# 倒计时最后这段时间要求目标在前台，不满足则冻结倒计时等待用户切回。
ARM_FINAL_WINDOW_S = 1.0
ARM_STABLE_S = 0.5


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
    # 自动变粗：绘制前扫描小程序粗细滑杆标定笔宽表，按明暗分档换笔绘制。
    auto_width: bool = False
    width_tiers: int = 5
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
            auto_width=bool(data.get("autoWidth", False)),
            width_tiers=int(data.get("widthTiers", 5)),
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
            "widthTiers": self.width_tiers,
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

        # 自动变粗：首绘先扫描滑杆标定笔宽表（刻度带测试线→清空画布），
        # 续画复用已回写的宽度表，保证分档笔序与首绘逐次一致。
        slider_now: dict | None = None
        undo_now: dict | None = None
        fold_now: dict | None = None
        if calibration.slider is not None:
            dx, dy = new_rect.x - saved_rect.x, new_rect.y - saved_rect.y
            slider_now = {**calibration.slider, "x": calibration.slider["x"] + dx,
                          "y": calibration.slider["y"] + dy}
            if calibration.undo is not None:
                undo_now = {**calibration.undo, "x": calibration.undo["x"] + dx,
                            "y": calibration.undo["y"] + dy}
            if calibration.fold is not None:
                fold_now = {**calibration.fold, "x": calibration.fold["x"] + dx,
                            "y": calibration.fold["y"] + dy}
        # 面板开合控制器：折叠按钮已标定时才启用（绘制期间保持折叠，画布完整可见）。
        panel: PanelController | None = None
        if params.auto_width and slider_now is not None and fold_now is not None:
            panel = PanelController(
                probe=target,
                mouse=mouse,
                to_screen=lambda x, y: image_point_to_screen(x, y, image_w, image_h, client),
            )
        if params.auto_width and params.width_table is None and cursor == 0:
            if panel is not None and panel.is_open(slider_now) is False:
                panel.unfold(slider_now, fold_now)
            params = self._run_pen_calibration(
                task_id, params, mouse, target, client, (image_w, image_h),
                new_rect, slider_now, undo_now, handle,
            )
            if params is None:  # 标定被停止，已落 STOPPED
                return

        # 开画前收起工具面板（滑杆不再需要常开）。面板位于画布下方的工具条，
        # 收起只腾出可视区域、不改变画布矩形，落笔映射沿用 new_rect。
        panel_active = panel is not None and params.width_table is not None
        if panel_active:
            panel.fold(slider_now, fold_now)

        flat = self._flatten_tiered_paths(task_id, paths, params)

        handle.guard_active.set()
        self._transition(task_id, "DRAWING")
        last_pct = -1
        point_delay_s = max(0.0, params.point_delay_ms) / 1000
        stroke_delay_s = max(0.0, params.stroke_delay_ms) / 1000
        active_tier: int | None = None

        for index in range(cursor, total):
            if handle.stop_event.is_set():
                self._finish_stopped(task_id, index, total, started, handle)
                return
            if not target.alive():
                raise TaskError(409, "E_WINDOW_LOST", "绘制期间目标窗口丢失")

            tier, canvas_path = flat[index]
            if (
                params.width_table
                and slider_now is not None
                and tier != active_tier
            ):
                # 换笔：点击滑杆到该档实测位置。置空静置光标，避免把滑杆点击
                # 误判成用户接管。面板折叠时需先展开再点滑杆、点完折回；展开
                # 失败则降级保持当前笔宽继续画，绝不盲点。
                handle.settled_cursor = None
                switched = True
                if panel_active and panel.is_open(slider_now) is False:
                    switched = panel.unfold(slider_now, fold_now)
                    if not switched:
                        self._emit(task_id, "task.warning", {
                            "message": f"工具面板无法展开，第 {tier + 1} 档沿用当前笔宽"
                        })
                if switched:
                    frac = params.width_table[min(tier, len(params.width_table) - 1)][0]
                    sx = slider_now["x"] + slider_now["w"] * frac
                    sy = slider_now["y"] + slider_now["h"] / 2.0
                    px, py = image_point_to_screen(sx, sy, image_w, image_h, client)
                    mouse.click(px, py)  # type: ignore[attr-defined]
                    time.sleep(0.25)
                    active_tier = tier
                    self._emit(task_id, "task.warning", {"message": f"已切换第 {tier + 1} 档笔宽"})
                if panel_active:
                    panel.fold(slider_now, fold_now)
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
                tiers=params.width_tiers,
                canvas=(canvas.x, canvas.y, canvas.w, canvas.h),
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

    def _flatten_tiered_paths(
        self,
        task_id: str,
        paths: list[list[tuple[float, float]]],
        params: ExecParams,
    ) -> list[tuple[int, list[tuple[float, float]]]]:
        """有宽度表时按局部笔迹密度分档重排（先粗后细），否则平铺为单档。"""
        tiers = len(params.width_table) if params.width_table else 0
        if tiers < 2:
            return [(0, p) for p in paths]
        groups = tier_groups(paths, tiers)
        return [(tier, path) for tier, group in groups for path in group]

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
