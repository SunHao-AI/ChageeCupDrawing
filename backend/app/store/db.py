"""SQLite 建库与连接管理。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS project (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    image_path TEXT NOT NULL,
    image_width INTEGER NOT NULL,
    image_height INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS canvas_profile (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    hwnd INTEGER NOT NULL,
    window_title TEXT NOT NULL DEFAULT '',
    capture_method TEXT NOT NULL DEFAULT '',
    rect_x INTEGER NOT NULL,
    rect_y INTEGER NOT NULL,
    rect_w INTEGER NOT NULL,
    rect_h INTEGER NOT NULL,
    dpi_scale REAL NOT NULL DEFAULT 1.0,
    source TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.0,
    captured_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_canvas_project
    ON canvas_profile(project_id, captured_at DESC);

CREATE TABLE IF NOT EXISTS plan (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    params TEXT NOT NULL,
    strategy TEXT NOT NULL,
    metrics TEXT NOT NULL,
    paths_path TEXT NOT NULL,
    target_w INTEGER NOT NULL,
    target_h INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_plan_project
    ON plan(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS draw_task (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES plan(id) ON DELETE CASCADE,
    exec_params TEXT NOT NULL,
    status TEXT NOT NULL,
    cursor_path INTEGER NOT NULL DEFAULT 0,
    stop_reason TEXT,
    error_code TEXT,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS task_event (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES draw_task(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    ts TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_task ON task_event(task_id, seq);

CREATE TABLE IF NOT EXISTS preset (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    params TEXT NOT NULL,
    builtin INTEGER NOT NULL DEFAULT 0
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
