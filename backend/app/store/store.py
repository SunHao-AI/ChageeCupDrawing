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
from app.store.models import CanvasProfile, PlanRecord, Preset, Project, utc_now

ASSET_KINDS = ("target", "simulated", "difference", "composite")

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
        self.tmp_dir = self.data_dir / "tmp"
        self._lock = threading.Lock()

    def init(self) -> None:
        for directory in (self.uploads_dir, self.plans_dir, self.tmp_dir):
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

    def new_project_id(self) -> str:
        return uuid.uuid4().hex

    def new_plan_id(self) -> str:
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
            conn.execute("DELETE FROM project WHERE id = ?", (project_id,))
        shutil.rmtree(self.project_dir(project_id), ignore_errors=True)
        for plan in self.list_plans(project_id):
            shutil.rmtree(self.plan_dir(plan.id), ignore_errors=True)

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
        return total

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
