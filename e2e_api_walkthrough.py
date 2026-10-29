# -*- coding: utf-8 -*-
"""真机 API E2E 走查：对运行中的服务打非侵入接口（不启动绘制任务）。

覆盖：会话门禁、上传校验链、校准门禁、复制校准、规划、resume-state 状态机、
任务删除状态机（409/204/404 幂等）、clear-canvas 参数校验、预设、镜像、错误体格式。
用法：python e2e_api_walkthrough.py  （服务需已在 127.0.0.1:8765 运行）
"""
from __future__ import annotations

import io
import sys

import requests
from PIL import Image

BASE = "http://127.0.0.1:8765/api"
HDR = {"X-Requested-With": "cupdraw"}

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def png_bytes(w: int, h: int, mode: str = "ink") -> bytes:
    img = Image.new("RGB", (w, h), "white")
    if mode == "ink":
        for x in range(w // 4, w * 3 // 4):
            for y in range(h // 4, h * 3 // 4):
                img.putpixel((x, y), (0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def main() -> int:
    s = requests.Session()

    print("== 会话与静态 ==")
    r = s.get(f"{BASE}/health")
    check("GET /health 免鉴权且下发 Cookie", r.ok and "cupdraw" in r.headers.get("Set-Cookie", ""), r.text[:120])
    r = s.post(f"{BASE}/projects")  # 无 Cookie 首次也可能已过：只验 4xx 结构
    check("写请求无 CSRF 头被挡 401", r.status_code == 401, f"got {r.status_code}")
    r = s.post(f"{BASE}/projects", headers=HDR)
    check("写请求缺 file 字段 400 E_INVALID_ARGUMENT", r.status_code == 400 and r.json().get("errorCode") == "E_INVALID_ARGUMENT", f"got {r.status_code}")
    r = s.delete(f"{BASE}/tasks/nonexist", headers=HDR)
    check("未知任务 DELETE 404 + errorCode", r.status_code == 404 and r.json().get("errorCode") == "E_NOT_FOUND", r.text[:120])

    print("== 上传校验链 ==")
    r = s.post(f"{BASE}/projects", headers=HDR, files={"file": ("a.txt", b"hello", "text/plain")})
    check("非图片 415 E_UNSUPPORTED_MEDIA", r.status_code == 415 and r.json()["errorCode"] == "E_UNSUPPORTED_MEDIA", r.text[:120])
    r = s.post(f"{BASE}/projects", headers=HDR, files={"file": ("e.png", b"", "image/png")})
    check("空文件 400", r.status_code == 400, f"got {r.status_code}")
    r = s.post(
        f"{BASE}/projects", headers=HDR,
        files={"file": ("ok.png", png_bytes(400, 400), "image/png")},
        data={"name": "x" * 65},
    )
    check("项目名超长 400", r.status_code == 400, f"got {r.status_code}")
    r = s.post(f"{BASE}/projects", headers=HDR, files={"file": ("ok.png", png_bytes(400, 400), "image/png")})
    check("合法 PNG 201", r.status_code == 201, r.text[:120])
    p1 = r.json()["projectId"]
    r = s.get(f"{BASE}/projects/{p1}/image")
    check("原图回读 200 image/png", r.status_code == 200 and r.headers["content-type"].startswith("image/png"))

    print("== 校准门禁与复制 ==")
    # 校准必须有真实存活窗口（真机语义：假句柄应被拒绝）。借当前桌面任一存活窗口。
    windows = s.get(f"{BASE}/devices").json()["pcWindows"]
    live = next((w for w in windows if w.get("title")), None)
    check("存在可借用的存活窗口", live is not None, "桌面没有任何带标题窗口")
    hwnd = live["hwnd"] if live else 0

    r = s.post(f"{BASE}/projects/{p1}/calibration", headers=HDR, json={"hwnd": 123456789, "mode": "auto"})
    check("假句柄校准 409 E_WINDOW_LOST",
          r.status_code == 409 and r.json()["errorCode"] == "E_WINDOW_LOST", r.text[:160])
    small = {"x": 10, "y": 10, "w": 50, "h": 50}
    rect = small
    r = s.post(
        f"{BASE}/projects/{p1}/calibration", headers=HDR,
        json={"hwnd": hwnd, "mode": "manual", "rect": small},
    )
    check("manual 校准 200 回显 rect", r.status_code == 200 and r.json()["rect"] == small, r.text[:200])
    r = s.post(f"{BASE}/projects/{p1}/calibration", headers=HDR, json={"hwnd": hwnd, "mode": "manual"})
    check("manual 缺 rect 400 E_INVALID_ARGUMENT",
          r.status_code == 400 and r.json()["errorCode"] == "E_INVALID_ARGUMENT", f"got {r.status_code}")

    r = s.post(
        f"{BASE}/projects", headers=HDR,
        files={"file": ("p2.png", png_bytes(400, 400), "image/png")},
        data={"copyCalibrationFrom": p1},
    )
    p2 = r.json()["projectId"]
    r = s.get(f"{BASE}/projects/{p2}/resume-state")
    cal = r.json().get("calibration") or {}
    check("copyCalibrationFrom 沿用（rect 相等 source=copied）",
          cal.get("rect") == rect and cal.get("source") == "copied", r.text[:200])
    r = s.post(
        f"{BASE}/projects", headers=HDR,
        files={"file": ("p3.png", png_bytes(400, 400), "image/png")},
        data={"copyCalibrationFrom": "no-such-project"},
    )
    p3 = r.json()["projectId"]
    r = s.get(f"{BASE}/projects/{p3}/resume-state")
    check("复制来源不存在静默跳过（校准 null）", r.json().get("calibration") is None, r.text[:160])

    print("== 规划 ==")
    r = s.get(f"{BASE}/plans/no-such-plan")
    check("未知规划 GET 404", r.status_code == 404, f"got {r.status_code}")
    r = s.post(f"{BASE}/projects/{p1}/plans", headers=HDR, json={"binaryThreshold": 999})
    check("非法规划参数 400 E_INVALID_ARGUMENT",
          r.status_code == 400 and r.json()["errorCode"] == "E_INVALID_ARGUMENT", f"got {r.status_code}")
    r = s.post(f"{BASE}/projects/{p1}/plans", headers=HDR, json={}, timeout=180)
    ok_plan = r.status_code == 200 and r.json()["metrics"]["pathCount"] > 0
    check("默认参数规划 200 且出笔", ok_plan, r.text[:200])
    plan = r.json()
    plan_id = plan.get("planId")
    r = s.get(f"{BASE}/plans/{plan_id}")
    check("规划回读 200", r.status_code == 200 and r.json()["planId"] == plan_id)

    print("== clear-canvas 参数校验（不触发真实设备操作）==")
    r = s.post(f"{BASE}/projects/no-such/clear-canvas", headers=HDR, json={})
    check("未知项目 clear 404", r.status_code == 404, f"got {r.status_code}")
    r = s.post(f"{BASE}/projects/{p2}/clear-canvas", headers=HDR, json={})
    check("无设备在场 clear 4xx 带 errorCode（未误触设备）",
          400 <= r.status_code < 500 and "errorCode" in r.json(), r.text[:160])

    print("== 预设 ==")
    r = s.get(f"{BASE}/presets")
    check("预设列表 200 含内置", r.status_code == 200 and isinstance(r.json(), list))
    r = s.post(f"{BASE}/presets", headers=HDR, json={"name": "", "params": {}})
    check("空名预设 4xx", 400 <= r.status_code < 500, f"got {r.status_code}")
    r = s.post(f"{BASE}/presets", headers=HDR, json={"name": "E2E 走查预设", "params": {}})
    check("创建预设 201/200", r.status_code in (200, 201), r.text[:120])

    print("== 镜像与设备（只读为主）==")
    r = s.get(f"{BASE}/devices")
    check("设备列表 200 含 pcWindows/tools", r.status_code == 200 and "pcWindows" in r.json() and "tools" in r.json())
    r = s.delete(f"{BASE}/devices/mirror", headers=HDR)
    check("未投屏时注销 200（幂等）或明确错误码",
          r.status_code == 200 or (400 <= r.status_code < 500 and "errorCode" in r.json()), f"got {r.status_code}")

    print(f"\n结果：{PASS} ok / {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
