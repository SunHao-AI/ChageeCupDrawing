"""I8 创建任务、I9 任务快照、I10 SSE 进度流、I11 停止、I12 续画。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.api.errors import ApiError
from app.api.schemas import TaskCreate, TaskResume
from app.executor.service import TERMINAL_STATUSES, TaskError
from app.store.models import TaskEvent, TaskRecord

router = APIRouter()

SSE_POLL_S = 0.05
SSE_HEARTBEAT_S = 15.0
# 终态落库与 task.done 事件之间存在毫秒级窗口，终态后有界重试直到 done 出现。
SSE_TERMINAL_DRAIN_TRIES = 20


def _snapshot(record: TaskRecord, total_paths: int) -> dict[str, object]:
    return {
        "taskId": record.id,
        "planId": record.plan_id,
        "status": record.status,
        "cursorPath": record.cursor_path,
        "totalPaths": total_paths,
        "exec": record.exec_params,
        "stopReason": record.stop_reason,
        "errorCode": record.error_code,
        "startedAt": record.started_at,
        "finishedAt": record.finished_at,
    }


def _sse(event: TaskEvent) -> str:
    return (
        f"id: {event.seq}\n"
        f"event: {event.type}\n"
        f"data: {json.dumps(event.payload, ensure_ascii=False)}\n\n"
    )


@router.post("/plans/{plan_id}/tasks", status_code=201)
def create_task(plan_id: str, payload: TaskCreate, request: Request) -> dict[str, object]:
    store = request.app.state.store
    plan = store.get_plan(plan_id)
    if plan is None:
        raise ApiError(404, "E_NOT_FOUND", "规划不存在")

    try:
        record, total_paths = request.app.state.executor.start(
            plan=plan,
            exec_params=payload.exec.model_dump(by_alias=True),
            countdown_ms=payload.countdown_ms,
            allow_drift=payload.allow_drift,
        )
    except TaskError as exc:
        raise ApiError(exc.status, exc.code, exc.message) from exc

    return {
        "taskId": record.id,
        "status": record.status,
        "planId": record.plan_id,
        "totalPaths": total_paths,
        "startedAt": record.started_at,
    }


@router.get("/tasks/{task_id}")
def get_task(task_id: str, request: Request) -> dict[str, object]:
    store = request.app.state.store
    record = store.get_task(task_id)
    if record is None:
        raise ApiError(404, "E_NOT_FOUND", "任务不存在")
    plan = store.get_plan(record.plan_id)
    total = int(plan.metrics.get("pathCount", 0)) if plan else 0
    return _snapshot(record, total)


@router.get("/tasks/{task_id}/events")
async def task_events(task_id: str, request: Request) -> StreamingResponse:
    store = request.app.state.store
    if store.get_task(task_id) is None:
        raise ApiError(404, "E_NOT_FOUND", "任务不存在")

    last_header = request.headers.get("last-event-id", "0")
    try:
        after_seq = int(last_header)
    except ValueError:
        after_seq = 0

    async def stream() -> AsyncIterator[str]:
        yield "retry: 2000\n\n"
        cursor = after_seq
        idle = 0.0
        seen_done = False
        while True:
            for event in store.list_events(task_id, cursor):
                cursor = event.seq
                idle = 0.0
                if event.type == "task.done":
                    seen_done = True
                yield _sse(event)

            record = store.get_task(task_id)
            if record is not None and record.status in TERMINAL_STATUSES:
                # task.done 可能晚于终态落库几毫秒，有界重试直到拿到或确认没有。
                for _ in range(1 if seen_done else SSE_TERMINAL_DRAIN_TRIES):
                    pending = store.list_events(task_id, cursor)
                    if not pending:
                        break
                    for event in pending:
                        cursor = event.seq
                        if event.type == "task.done":
                            seen_done = True
                        yield _sse(event)
                    if seen_done:
                        break
                    await asyncio.sleep(SSE_POLL_S)
                return

            await asyncio.sleep(SSE_POLL_S)
            idle += SSE_POLL_S
            if idle >= SSE_HEARTBEAT_S:
                idle = 0.0
                yield ": ping\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/tasks/{task_id}/stop", status_code=202)
def stop_task(task_id: str, request: Request) -> dict[str, object]:
    try:
        record = request.app.state.executor.stop(task_id)
    except TaskError as exc:
        raise ApiError(exc.status, exc.code, exc.message) from exc
    return {"taskId": record.id, "status": record.status}


@router.post("/tasks/{task_id}/resume", status_code=202)
def resume_task(task_id: str, payload: TaskResume, request: Request) -> dict[str, object]:
    store = request.app.state.store
    try:
        record, total_paths = request.app.state.executor.resume(
            task_id=task_id,
            countdown_ms=payload.countdown_ms,
            allow_drift=payload.allow_drift,
        )
    except TaskError as exc:
        raise ApiError(exc.status, exc.code, exc.message) from exc

    return {
        "taskId": record.id,
        "status": record.status,
        "planId": record.plan_id,
        "cursorPath": record.cursor_path,
        "totalPaths": total_paths,
        "startedAt": record.started_at,
    }
