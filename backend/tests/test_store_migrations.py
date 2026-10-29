"""存储层迁移与校准新字段读写回归：展开/折叠双态工具位图、任务删除级联。"""

from __future__ import annotations

import sqlite3

from app.store.db import init_db
from app.store.store import Store


def test_tools_bitmaps_roundtrip(tmp_path) -> None:
    store = Store(tmp_path)
    store.init()
    store.create_project(
        project_id="p1", name="s.png", image_path=tmp_path / "s.png",
        image_width=10, image_height=10,
    )
    bit = {"brush": {"x": 10, "y": 500}, "eraser": {"x": 60, "y": 500},
           "slider": {"x": 100, "y": 520, "w": 200, "h": 12}, "undo": None,
           "fold": {"x": 300, "y": 500}, "brushSelectedLum": 180.0,
           "brushIdleLum": 250.0}
    store.save_calibration(
        project_id="p1", hwnd=1, window_title="t", capture_method="wgc",
        rect_x=0, rect_y=0, rect_w=100, rect_h=100, dpi_scale=1.0,
        source="manual", confidence=1.0, tools_open=bit, tools_collapsed=None,
    )
    p = store.latest_calibration("p1")
    assert p.tools_open == bit
    assert p.tools_collapsed is None


def test_legacy_db_gains_tools_columns(tmp_path) -> None:
    """旧库文件（无新列）经 init_db 的 ALTER 补列机制自动升级并可继续读写。"""
    db_path = tmp_path / "cupdraw.db"
    conn = sqlite3.connect(db_path)
    # 模拟旧版 canvas_profile 表：slider/undo/fold 之前、无 device_*、无 tools_*。
    conn.execute(
        "CREATE TABLE canvas_profile ("
        " id TEXT PRIMARY KEY, project_id TEXT NOT NULL,"
        " hwnd INTEGER NOT NULL, window_title TEXT NOT NULL DEFAULT '',"
        " capture_method TEXT NOT NULL DEFAULT '',"
        " rect_x INTEGER NOT NULL, rect_y INTEGER NOT NULL,"
        " rect_w INTEGER NOT NULL, rect_h INTEGER NOT NULL,"
        " dpi_scale REAL NOT NULL DEFAULT 1.0, source TEXT NOT NULL,"
        " confidence REAL NOT NULL DEFAULT 0.0,"
        " slider_json TEXT, undo_json TEXT, fold_json TEXT, captured_at TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()

    init_db(db_path)

    columns = {row[1] for row in sqlite3.connect(db_path).execute("PRAGMA table_info(canvas_profile)")}
    assert {"tools_open_json", "tools_collapsed_json"} <= columns

    store = Store(tmp_path)
    store.init()
    store.create_project(
        project_id="p1", name="s.png", image_path=tmp_path / "s.png",
        image_width=10, image_height=10,
    )
    # 旧调用方式（不传新字段）必须继续工作，读回时为 None。
    store.save_calibration(
        project_id="p1", hwnd=1, window_title="t", capture_method="wgc",
        rect_x=0, rect_y=0, rect_w=100, rect_h=100, dpi_scale=1.0,
        source="manual", confidence=1.0,
    )
    p = store.latest_calibration("p1")
    assert p.tools_open is None
    assert p.tools_collapsed is None


def test_delete_task_cascades_events_and_keeps_plan(tmp_path) -> None:
    """删除任务：事件随外键级联清空，规划/校准原封不动（删除任务回初始态）。"""
    store = Store(tmp_path)
    store.init()
    store.create_project(
        project_id="p1", name="s.png", image_path=tmp_path / "s.png",
        image_width=10, image_height=10,
    )
    store.save_calibration(
        project_id="p1", hwnd=1, window_title="t", capture_method="wgc",
        rect_x=0, rect_y=0, rect_w=100, rect_h=100, dpi_scale=1.0,
        source="manual", confidence=1.0,
    )
    paths = tmp_path / "paths.json"
    paths.write_text("[]", encoding="utf-8")
    store.create_plan(
        plan_id="plan1", project_id="p1", params={}, strategy="basic",
        metrics={"pathCount": 3}, paths_path=paths, target_w=10, target_h=10,
    )
    task = store.create_task(plan_id="plan1", exec_params={})
    store.append_event(task.id, "task.state", {"status": "DRAWING"})
    store.update_task(task.id, status="STOPPED", stop_reason="pause")

    store.delete_task(task.id)

    assert store.get_task(task.id) is None
    assert store.list_events(task.id) == []
    # 回的是「初始状态」而不是「什么都没做过」：规划与校准仍在。
    assert store.get_plan("plan1") is not None
    assert store.latest_calibration("p1") is not None
    assert store.latest_task_for_project("p1") is None
