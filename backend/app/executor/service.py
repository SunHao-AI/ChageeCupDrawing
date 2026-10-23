"""执行器服务：任务状态机、倒计时、漂移校验、绘制线程与 ESC 守卫。

状态只由本服务单向推进（design 6.1），API 层只能发出停止意图。
进度以笔迹条数为单位；每完成一条笔迹持久化 cursor_path，约每 2% 推送一次事件。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.core.vision.canvas_detect import detect_canvas_rect
from app.device.capture import CaptureError
from app.device.geometry import Rect, image_point_to_screen
from app.device.service import DeviceService
from app.executor.native_mouse import NativeMouse, path_length
from app.store.models import PlanRecord, TaskRecord, utc_now
from app.store.store import Store

TERMINAL_STATUSES = ("DONE", "FAILED", "STOPPED")

ESCAPE_POLL_S = 0.015


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
        }


@dataclass
class _RunHandle:
    """单次执行的停止信号；生命周期与线程严格绑定，避免跨任务串扰。"""

    stop_event: threading.Event = field(default_factory=threading.Event)
    guard_event: threading.Event = field(default_factory=threading.Event)
    reason: str | None = None

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
    ) -> None:
        self._store = store
        self._device = device
        self._mouse_factory = mouse_factory
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
        hwnd = self._calibrated_hwnd(plan)
        handle, mouse = self._claim(hwnd)

        try:
            record = self._store.create_task(plan_id=plan.id, exec_params=params.as_dict())
            self._arm(record.id)
            record = self._store.get_task(record.id) or record
        except BaseException:
            self._release(handle)
            raise

        self._spawn(record, plan, paths, params, mouse, hwnd, countdown_ms, allow_drift, handle=handle)
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
        hwnd = self._calibrated_hwnd(plan)
        handle, mouse = self._claim(hwnd)

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
            hwnd,
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

    def _calibrated_hwnd(self, plan: PlanRecord) -> int:
        calibration = self._store.latest_calibration(plan.project_id)
        if calibration is None or calibration.rect_w <= 0 or calibration.rect_h <= 0:
            raise TaskError(422, "E_CANVAS_MISSING", "项目尚未完成画布校准")
        return calibration.hwnd

    def _claim(self, hwnd: int) -> tuple[_RunHandle, object]:
        """占用全局唯一执行槽位并构造鼠标；失败抛 TaskError。"""
        with self._lock:
            if self._handle is not None:
                raise TaskError(409, "E_TASK_CONFLICT", "已有绘制任务在执行")
            if not self._device.alive(hwnd):
                raise TaskError(409, "E_WINDOW_LOST", "目标窗口已关闭或句柄失效")
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
        hwnd: int,
        countdown_ms: int,
        allow_drift: float,
        handle: _RunHandle,
        cursor: int = 0,
    ) -> None:
        threading.Thread(
            target=self._run,
            args=(record.id, plan, paths, params, mouse, hwnd, countdown_ms, allow_drift, cursor, handle),
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
        hwnd: int,
        countdown_ms: int,
        allow_drift: float,
        cursor: int,
        handle: _RunHandle,
    ) -> None:
        started = time.monotonic()
        total = len(paths)
        self._start_escape_guard(mouse, handle)
        try:
            self._arm_and_draw(
                task_id, plan, paths, params, mouse, hwnd,
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
        hwnd: int,
        countdown_ms: int,
        allow_drift: float,
        cursor: int,
        total: int,
        started: float,
        handle: _RunHandle,
    ) -> None:
        calibration = self._store.latest_calibration(plan.project_id)
        assert calibration is not None

        # 倒计时给用户切到目标窗口，可被停止打断（ARMING → STOPPED）。
        if handle.stop_event.wait(max(0, countdown_ms) / 1000):
            self._finish_stopped(task_id, cursor, total, started, handle)
            return

        if not self._device.alive(hwnd):
            raise TaskError(409, "E_WINDOW_LOST", "倒计时期间目标窗口丢失")
        if self._device.foreground() != hwnd:
            raise TaskError(409, "E_FOREGROUND_MISMATCH", "前台窗口与目标不一致，未落笔")

        try:
            capture = self._device.capture(hwnd)
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

        client = self._device.client_area(hwnd)

        self._transition(task_id, "DRAWING")
        last_pct = -1
        point_delay_s = max(0.0, params.point_delay_ms) / 1000
        stroke_delay_s = max(0.0, params.stroke_delay_ms) / 1000

        for index in range(cursor, total):
            if handle.stop_event.is_set():
                self._finish_stopped(task_id, index, total, started, handle)
                return
            if not self._device.alive(hwnd):
                raise TaskError(409, "E_WINDOW_LOST", "绘制期间目标窗口丢失")

            canvas_path = paths[index]
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
            if len(screen_path) == 1 or path_length(screen_path) < params.short_stroke_threshold_px:
                hold = max(0.010, params.click_hold_ms / 1000)
                if params.short_stroke_protect:
                    hold = max(hold, 0.022)
                mouse.click(*screen_path[0], hold=hold)  # type: ignore[attr-defined]
            else:
                completed = mouse.drag_polyline(  # type: ignore[attr-defined]
                    screen_path,
                    event_step_px=params.event_step_px,
                    point_delay_s=point_delay_s,
                    should_stop=handle.stop_event.is_set,
                    no_coalesce=params.no_coalesce,
                    short_stroke_protect=params.short_stroke_protect,
                )
                if not completed:
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

    def _start_escape_guard(self, mouse: object, handle: _RunHandle) -> None:
        try:
            mouse.clear_escape_state()  # type: ignore[attr-defined]
        except Exception:
            return

        def poll() -> None:
            while not handle.guard_event.is_set():
                try:
                    if mouse.esc_pressed():  # type: ignore[attr-defined]
                        handle.request_stop("esc")
                        return
                except Exception:
                    return
                time.sleep(ESCAPE_POLL_S)

        threading.Thread(target=poll, daemon=True).start()
