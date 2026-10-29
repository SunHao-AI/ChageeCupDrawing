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
    source_version INTEGER NOT NULL DEFAULT 1,
    parent_version INTEGER,
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
    frame_w INTEGER NOT NULL DEFAULT 0,
    frame_h INTEGER NOT NULL DEFAULT 0,
    dpi_scale REAL NOT NULL DEFAULT 1.0,
    source TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.0,
    slider_json TEXT,
    undo_json TEXT,
    fold_json TEXT,
    tools_open_json TEXT,
    tools_collapsed_json TEXT,
    blank_baseline_json TEXT,
    snapshot_id TEXT,
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

CREATE TABLE IF NOT EXISTS edit_draft (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    base_version INTEGER NOT NULL,
    channel TEXT NOT NULL,
    instruction TEXT NOT NULL,
    roi_json TEXT,
    provider TEXT NOT NULL,
    draft_path TEXT NOT NULL,
    diff_path TEXT,
    created_at TEXT NOT NULL,
    applied_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_edit_project
    ON edit_draft(project_id, created_at DESC);
"""

# 旧库升级用：SCHEMA 里的 CREATE TABLE IF NOT EXISTS 不会给已存在的表补列。
_COLUMN_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "project": [
        ("source_version", "INTEGER NOT NULL DEFAULT 1"),
        ("parent_version", "INTEGER"),
    ],
    "canvas_profile": [
        # 自动变粗需要：粗细滑杆轨道区域与撤销按钮位置（截图坐标系 JSON）。
        ("slider_json", "TEXT"),
        ("undo_json", "TEXT"),
        # 画板下方工具面板的折叠按钮中心点（换笔间隙收起面板用）。
        ("fold_json", "TEXT"),
        # 面板展开/折叠两态工具位图（逐段笔宽：探测产物 JSON）。
        ("tools_open_json", "TEXT"),
        ("tools_collapsed_json", "TEXT"),
        # 多设备目标（B2）：pc / mirror / adb 与来源设备序列号。
        ("device_kind", "TEXT NOT NULL DEFAULT 'pc'"),
        ("device_serial", "TEXT"),
        # 空白画布基线掩码（canvas_blankness L2）。
        ("blank_baseline_json", "TEXT"),
        # 自动校准快照 ID（标注层锚帧用）。
        ("snapshot_id", "TEXT"),
        # 产出本份校准那一刻的截图帧尺寸：标注层坐标系据此对齐，
        # 不再回退实时截图 naturalSize（两帧不一致会让 RectEditor 缩放失真）。
        ("frame_w", "INTEGER NOT NULL DEFAULT 0"),
        ("frame_h", "INTEGER NOT NULL DEFAULT 0"),
    ],
}


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _COLUMN_MIGRATIONS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue
        for name, ddl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate_columns(conn)
        conn.commit()
    finally:
        conn.close()
