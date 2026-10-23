"""SQLite 仓储与资产目录访问。"""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.store.assets import SavedImage, save_source_image  # noqa: F401  (对外导出)
from app.store.db import connect, init_db
from app.store.models import (
    CanvasProfile,
    EditDraft,
    PlanRecord,
    Preset,
    Project,
    TaskEvent,
    TaskRecord,
    utc_now,
)

ASSET_KINDS = ("target", "simulated", "difference", "composite")

# design 5.2：每任务事件上限，超出丢最早的 task.progress，保留全部状态迁移。
MAX_TASK_EVENTS = 2000

# 磁盘文件名沿用 design 5.2 的约定：目标掩膜叫 mask.png。
ASSET_FILES = {
    "target": "mask.png",
    "simulated": "simulated.png",
    "difference": "difference.png",
    "composite": "composite.png",
}


class Store:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "cupdraw.db"
        self.uploads_dir = self.data_dir / "uploads"
        self.plans_dir = self.data_dir / "plans"
        self.edits_dir = self.data_dir / "edits"
        self.tmp_dir = self.data_dir / "tmp"
        self._lock = threading.Lock()

    def init(self) -> None:
        for directory in (self.uploads_dir, self.plans_dir, self.edits_dir, self.tmp_dir):
            directory.mkdir(parents=True, exist_ok=True)
        init_db(self.db_path)

    # ---------- 目录约定 ----------

    def project_dir(self, project_id: str) -> Path:
        return self.uploads_dir / project_id

    def plan_dir(self, plan_id: str) -> Path:
        return self.plans_dir / plan_id

    def plan_asset_path(self, plan_id: str, kind: str) -> Path:
        if kind not in ASSET_FILES:
            raise ValueError(f"未知资产类型：{kind}")
        return self.plan_dir(plan_id) / ASSET_FILES[kind]

    def plan_paths_path(self, plan_id: str) -> Path:
        return self.plan_dir(plan_id) / "paths.json"

    def edit_dir(self, edit_id: str) -> Path:
        return self.edits_dir / edit_id

    def new_project_id(self) -> str:
        return uuid.uuid4().hex

    def new_plan_id(self) -> str:
        return uuid.uuid4().hex

    def new_edit_id(self) -> str:
        return uuid.uuid4().hex

    # ---------- 连接 ----------

    @contextmanager
    def _open(self) -> Iterator[sqlite3.Connection]:
        conn = connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = connect(self.db_path)
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    # ---------- 项目 ----------

    def create_project(
        self,
        *,
        project_id: str,
        name: str,
        image_path: Path,
        image_width: int,
        image_height: int,
    ) -> Project:
        created_at = utc_now()
        with self._write() as conn:
            conn.execute(
                "INSERT INTO project (id, name, image_path, image_width, image_height, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    name,
                    str(image_path),
                    int(image_width),
                    int(image_height),
                    created_at,
                ),
            )
        return Project(
            id=project_id,
            name=name,
            image_path=str(image_path),
            image_width=int(image_width),
            image_height=int(image_height),
            created_at=created_at,
        )

    def get_project(self, project_id: str) -> Project | None:
        with self._open() as conn:
            row = conn.execute("SELECT * FROM project WHERE id = ?", (project_id,)).fetchone()
        return _project(row) if row else None

    def delete_project(self, project_id: str) -> None:
        with self._write() as conn:
            rows = conn.execute(
                "SELECT id FROM edit_draft WHERE project_id = ?", (project_id,)
            ).fetchall()
            conn.execute("DELETE FROM project WHERE id = ?", (project_id,))
        shutil.rmtree(self.project_dir(project_id), ignore_errors=True)
        for plan in self.list_plans(project_id):
            shutil.rmtree(self.plan_dir(plan.id), ignore_errors=True)
        for row in rows:
            shutil.rmtree(self.edit_dir(row["id"]), ignore_errors=True)

    # ---------- 画布校准 ----------

    def save_calibration(
        self,
        *,
        project_id: str,
        hwnd: int,
        window_title: str,
        capture_method: str,
        rect_x: int,
        rect_y: int,
        rect_w: int,
        rect_h: int,
        dpi_scale: float,
        source: str,
        confidence: float,
    ) -> CanvasProfile:
        profile = CanvasProfile(
            id=uuid.uuid4().hex,
            project_id=project_id,
            hwnd=int(hwnd),
            window_title=window_title,
            capture_method=capture_method,
            rect_x=int(rect_x),
            rect_y=int(rect_y),
            rect_w=int(rect_w),
            rect_h=int(rect_h),
            dpi_scale=float(dpi_scale),
            source=source,
            confidence=float(confidence),
            captured_at=utc_now(),
        )
        with self._write() as conn:
            conn.execute(
                "INSERT INTO canvas_profile (id, project_id, hwnd, window_title, capture_method,"
                " rect_x, rect_y, rect_w, rect_h, dpi_scale, source, confidence, captured_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    profile.id,
                    profile.project_id,
                    profile.hwnd,
                    profile.window_title,
                    profile.capture_method,
                    profile.rect_x,
                    profile.rect_y,
                    profile.rect_w,
                    profile.rect_h,
                    profile.dpi_scale,
                    profile.source,
                    profile.confidence,
                    profile.captured_at,
                ),
            )
        return profile

    def latest_calibration(self, project_id: str) -> CanvasProfile | None:
        with self._open() as conn:
            row = conn.execute(
                "SELECT * FROM canvas_profile WHERE project_id = ?"
                " ORDER BY captured_at DESC, rowid DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        return _canvas(row) if row else None

    # ---------- 规划 ----------

    def create_plan(
        self,
        *,
        plan_id: str,
        project_id: str,
        params: dict,
        strategy: str,
        metrics: dict,
        paths_path: Path,
        target_w: int,
        target_h: int,
    ) -> PlanRecord:
        created_at = utc_now()
        with self._write() as conn:
            conn.execute(
                "INSERT INTO plan (id, project_id, params, strategy, metrics, paths_path,"
                " target_w, target_h, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    plan_id,
                    project_id,
                    json.dumps(params, ensure_ascii=False),
                    strategy,
                    json.dumps(metrics, ensure_ascii=False),
                    str(paths_path),
                    int(target_w),
                    int(target_h),
                    created_at,
                ),
            )
        return PlanRecord(
            id=plan_id,
            project_id=project_id,
            params=params,
            strategy=strategy,
            metrics=metrics,
            paths_path=str(paths_path),
            target_w=int(target_w),
            target_h=int(target_h),
            created_at=created_at,
        )

    def get_plan(self, plan_id: str) -> PlanRecord | None:
        with self._open() as conn:
            row = conn.execute("SELECT * FROM plan WHERE id = ?", (plan_id,)).fetchone()
        return _plan(row) if row else None

    def list_plans(self, project_id: str) -> list[PlanRecord]:
        with self._open() as conn:
            rows = conn.execute(
                "SELECT * FROM plan WHERE project_id = ? ORDER BY created_at DESC",
                (project_id,),
            ).fetchall()
        return [_plan(row) for row in rows]

    def project_asset_bytes(self, project_id: str) -> int:
        total = 0
        for path in _iter_files(self.project_dir(project_id)):
            total += path.stat().st_size
        for plan in self.list_plans(project_id):
            for path in _iter_files(self.plan_dir(plan.id)):
                total += path.stat().st_size
        for draft in self.list_edit_drafts(project_id):
            for path in _iter_files(self.edit_dir(draft.id)):
                total += path.stat().st_size
        return total

    # ---------- 绘制任务 ----------

    def create_task(self, *, plan_id: str, exec_params: dict) -> TaskRecord:
        task_id = uuid.uuid4().hex
        with self._write() as conn:
            conn.execute(
                "INSERT INTO draw_task (id, plan_id, exec_params, status, cursor_path)"
                " VALUES (?, ?, ?, 'PENDING', 0)",
                (task_id, plan_id, json.dumps(exec_params, ensure_ascii=False)),
            )
        record = self.get_task(task_id)
        assert record is not None
        return record

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._open() as conn:
            row = conn.execute("SELECT * FROM draw_task WHERE id = ?", (task_id,)).fetchone()
        return _task(row) if row else None

    def active_task(self) -> TaskRecord | None:
        with self._open() as conn:
            row = conn.execute(
                "SELECT * FROM draw_task WHERE status IN ('PENDING','ARMING','DRAWING','STOPPING')"
                " ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        return _task(row) if row else None

    def update_task(self, task_id: str, **fields: object) -> None:
        if not fields:
            return
        allowed = {
            "status", "cursor_path", "stop_reason", "error_code", "started_at", "finished_at"
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"非法任务字段：{sorted(unknown)}")
        columns = ", ".join(f"{key} = ?" for key in fields)
        with self._write() as conn:
            conn.execute(
                f"UPDATE draw_task SET {columns} WHERE id = ?",
                (*fields.values(), task_id),
            )

    def reopen_task(self, task_id: str) -> None:
        with self._write() as conn:
            conn.execute(
                "UPDATE draw_task SET status = 'PENDING', stop_reason = NULL,"
                " error_code = NULL, finished_at = NULL WHERE id = ?",
                (task_id,),
            )

    def mark_stopping(self, task_id: str) -> None:
        """仅 DRAWING 显式进入 STOPPING；其余状态由执行线程落终态。"""
        with self._write() as conn:
            conn.execute(
                "UPDATE draw_task SET status = 'STOPPING' WHERE id = ? AND status = 'DRAWING'",
                (task_id,),
            )

    # ---------- 任务事件 ----------

    def append_event(self, task_id: str, event_type: str, payload: dict) -> int:
        ts = utc_now()
        with self._write() as conn:
            cur = conn.execute(
                "INSERT INTO task_event (task_id, type, payload, ts) VALUES (?, ?, ?, ?)",
                (task_id, event_type, json.dumps(payload, ensure_ascii=False), ts),
            )
            seq = int(cur.lastrowid or 0)
            count = conn.execute(
                "SELECT COUNT(*) FROM task_event WHERE task_id = ?", (task_id,)
            ).fetchone()[0]
            overflow = int(count) - MAX_TASK_EVENTS
            if overflow > 0:
                conn.execute(
                    "DELETE FROM task_event WHERE seq IN ("
                    " SELECT seq FROM task_event WHERE task_id = ? AND type = 'task.progress'"
                    " ORDER BY seq LIMIT ?)",
                    (task_id, overflow),
                )
        return seq

    def list_events(self, task_id: str, after_seq: int = 0) -> list[TaskEvent]:
        with self._open() as conn:
            rows = conn.execute(
                "SELECT * FROM task_event WHERE task_id = ? AND seq > ? ORDER BY seq",
                (task_id, after_seq),
            ).fetchall()
        return [_event(row) for row in rows]

    # ---------- 编辑草稿与版本链 ----------

    def create_edit_draft(
        self,
        *,
        edit_id: str,
        project_id: str,
        base_version: int,
        channel: str,
        instruction: str,
        roi: dict | None,
        provider: str,
        draft_path: Path,
        diff_path: Path | None,
    ) -> EditDraft:
        created_at = utc_now()
        with self._write() as conn:
            conn.execute(
                "INSERT INTO edit_draft (id, project_id, base_version, channel, instruction,"
                " roi_json, provider, draft_path, diff_path, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    edit_id,
                    project_id,
                    int(base_version),
                    channel,
                    instruction,
                    json.dumps(roi, ensure_ascii=False) if roi is not None else None,
                    provider,
                    str(draft_path),
                    str(diff_path) if diff_path is not None else None,
                    created_at,
                ),
            )
        return EditDraft(
            id=edit_id,
            project_id=project_id,
            base_version=int(base_version),
            channel=channel,
            instruction=instruction,
            roi=roi,
            provider=provider,
            draft_path=str(draft_path),
            diff_path=str(diff_path) if diff_path is not None else None,
            created_at=created_at,
            applied_at=None,
        )

    def get_edit_draft(self, edit_id: str) -> EditDraft | None:
        with self._open() as conn:
            row = conn.execute("SELECT * FROM edit_draft WHERE id = ?", (edit_id,)).fetchone()
        return _edit(row) if row else None

    def list_edit_drafts(self, project_id: str) -> list[EditDraft]:
        with self._open() as conn:
            rows = conn.execute(
                "SELECT * FROM edit_draft WHERE project_id = ? ORDER BY created_at DESC",
                (project_id,),
            ).fetchall()
        return [_edit(row) for row in rows]

    def delete_edit_draft(self, edit_id: str) -> None:
        with self._write() as conn:
            conn.execute("DELETE FROM edit_draft WHERE id = ?", (edit_id,))
        shutil.rmtree(self.edit_dir(edit_id), ignore_errors=True)

    def apply_edit_draft(
        self,
        *,
        edit_id: str,
        new_image_path: Path,
        image_width: int,
        image_height: int,
    ) -> tuple[Project, EditDraft] | None:
        """apply 事务：指针切到新版本 + 草稿标记 applied，任何一步失败整体回滚。

        base_version 必须等于项目当前版本，否则说明项目已被再次编辑，返回 None。
        """
        draft = self.get_edit_draft(edit_id)
        if draft is None or draft.applied_at is not None:
            return None
        applied_at = utc_now()
        with self._write() as conn:
            cur = conn.execute(
                "UPDATE project SET image_path = ?, image_width = ?, image_height = ?,"
                " source_version = source_version + 1, parent_version = ?"
                " WHERE id = ? AND source_version = ?",
                (
                    str(new_image_path),
                    int(image_width),
                    int(image_height),
                    draft.base_version,
                    draft.project_id,
                    draft.base_version,
                ),
            )
            if cur.rowcount != 1:
                return None
            conn.execute(
                "UPDATE edit_draft SET applied_at = ? WHERE id = ? AND applied_at IS NULL",
                (applied_at, edit_id),
            )
        project = self.get_project(draft.project_id)
        updated = self.get_edit_draft(edit_id)
        assert project is not None and updated is not None
        return project, updated

    # ---------- 预设 ----------

    def create_preset(self, *, name: str, params: dict) -> Preset:
        preset = Preset(id=uuid.uuid4().hex, name=name, params=params, builtin=False)
        with self._write() as conn:
            conn.execute(
                "INSERT INTO preset (id, name, params, builtin) VALUES (?, ?, ?, ?)",
                (preset.id, preset.name, json.dumps(params, ensure_ascii=False), 0),
            )
        return preset

    def list_presets(self) -> list[Preset]:
        with self._open() as conn:
            rows = conn.execute("SELECT * FROM preset ORDER BY name").fetchall()
        return [_preset(row) for row in rows]


def _iter_files(directory: Path) -> Iterator[Path]:
    if not directory.is_dir():
        return
    for path in directory.rglob("*"):
        if path.is_file():
            yield path


def _project(row: sqlite3.Row) -> Project:
    return Project(
        id=row["id"],
        name=row["name"],
        image_path=row["image_path"],
        image_width=row["image_width"],
        image_height=row["image_height"],
        created_at=row["created_at"],
        source_version=int(row["source_version"]),
        parent_version=(int(row["parent_version"]) if row["parent_version"] is not None else None),
    )


def _edit(row: sqlite3.Row) -> EditDraft:
    return EditDraft(
        id=row["id"],
        project_id=row["project_id"],
        base_version=int(row["base_version"]),
        channel=row["channel"],
        instruction=row["instruction"],
        roi=json.loads(row["roi_json"]) if row["roi_json"] else None,
        provider=row["provider"],
        draft_path=row["draft_path"],
        diff_path=row["diff_path"],
        created_at=row["created_at"],
        applied_at=row["applied_at"],
    )


def _canvas(row: sqlite3.Row) -> CanvasProfile:
    return CanvasProfile(
        id=row["id"],
        project_id=row["project_id"],
        hwnd=row["hwnd"],
        window_title=row["window_title"],
        capture_method=row["capture_method"],
        rect_x=row["rect_x"],
        rect_y=row["rect_y"],
        rect_w=row["rect_w"],
        rect_h=row["rect_h"],
        dpi_scale=row["dpi_scale"],
        source=row["source"],
        confidence=row["confidence"],
        captured_at=row["captured_at"],
    )


def _plan(row: sqlite3.Row) -> PlanRecord:
    return PlanRecord(
        id=row["id"],
        project_id=row["project_id"],
        params=json.loads(row["params"]),
        strategy=row["strategy"],
        metrics=json.loads(row["metrics"]),
        paths_path=row["paths_path"],
        target_w=row["target_w"],
        target_h=row["target_h"],
        created_at=row["created_at"],
    )


def _preset(row: sqlite3.Row) -> Preset:
    return Preset(
        id=row["id"],
        name=row["name"],
        params=json.loads(row["params"]),
        builtin=bool(row["builtin"]),
    )


def _task(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        id=row["id"],
        plan_id=row["plan_id"],
        exec_params=json.loads(row["exec_params"]),
        status=row["status"],
        cursor_path=int(row["cursor_path"]),
        stop_reason=row["stop_reason"],
        error_code=row["error_code"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _event(row: sqlite3.Row) -> TaskEvent:
    return TaskEvent(
        seq=int(row["seq"]),
        task_id=row["task_id"],
        type=row["type"],
        payload=json.loads(row["payload"]),
        ts=row["ts"],
    )
