r"""B2 无手机全链路验证：注册窗口为镜像目标 → 校准 → 规划 → 绘制 → 守护 → 断点续画。

用独立 Chrome --app 窗口模拟「投屏目标」：页面是白底画布（pointer 事件落墨，
笔画数写进 document.title），后端经 /api/devices/mirror/register 登记其句柄，
之后校准/规划/执行/守护全部走与真机 scrcpy 完全相同的代码路径。

用法（需要后端已在 8765 运行）：
    python tests/manual/mirror_e2e/run_mirror_e2e.py

任一断言失败以非零码退出；输出逐步骤 PASS/FAIL。
"""

from __future__ import annotations

import ctypes
import http.server
import json
import math
import os
import re
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
from PIL import Image, ImageDraw

BASE = os.environ.get("CUPDRAW_E2E_BASE", "http://127.0.0.1:8765")
HERE = Path(__file__).resolve().parent
TITLE_PREFIX = "CupDraw E2E Target"
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]

user32 = ctypes.WinDLL("user32", use_last_error=True)
SW_MINIMIZE, SW_RESTORE = 6, 9

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILED.append(name)
    return ok


def wait_for(fn, timeout_s: float, what: str, interval_s: float = 0.1):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(interval_s)
    raise TimeoutError(f"等待超时：{what}")


# ---------- 夹具 ----------

def make_source_png(path: Path) -> None:
    """「一对紧邻线 + 两条孤立线」：4 条路径，但局部线密度必须有区分度。

    笔宽分桶按 21px 窗口内的局部线密度做百分位排名；若各线等长等距（密度完全
    相同），密度标准差为 0，规划会把所有段都算成 0 行程——只剩 1 个笔宽桶，
    「换宽确实发生」断言必然失败。所以前两条线相距 14px（彼此落在对方的密度
    窗口内 → 密度高），后两条各自孤立（密度低）。
    线数仍为 4：每档换宽都要「展开→点滑杆→折叠」两次折叠点击，档数过多会
    顶到夹具的 21 次上限。
    """
    img = Image.new("RGB", (420, 620), "white")
    draw = ImageDraw.Draw(img)
    for y in (100, 114, 250, 330):
        draw.line([(70, y), (350, y)], fill="black", width=3)
    img.save(path)


def start_canvas_server() -> tuple[socketserver.TCPServer, int]:
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(HERE), **kwargs)

        def log_message(self, *args):  # 静音
            pass

    for port in range(8899, 8910):
        try:
            server = socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler)
        except OSError:
            continue
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, port
    raise RuntimeError("无可用端口")


def launch_chrome(port: int, profile_dir: Path) -> subprocess.Popen:
    exe = next((p for p in CHROME_CANDIDATES if Path(p).is_file()), None)
    if exe is None:
        raise RuntimeError("未找到 Chrome/Edge")
    return subprocess.Popen(
        [
            exe,
            f"--app=http://127.0.0.1:{port}/canvas.html",
            f"--user-data-dir={profile_dir}",
            "--window-size=640,780",
            "--window-position=80,40",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--hide-crash-restore-bubble",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# ---------- API 封装 ----------

def client() -> httpx.Client:
    c = httpx.Client(base_url=f"{BASE}/api", timeout=30.0)
    c.get("/health")  # 拿会话 cookie
    return c


def post(c: httpx.Client, path: str, **kwargs) -> httpx.Response:
    return c.post(path, headers={"X-Requested-With": "cupdraw"}, **kwargs)


def task_status(c: httpx.Client, task_id: str) -> dict:
    return c.get(f"/tasks/{task_id}").json()


def strokes_of(window_title: str) -> int:
    m = re.search(re.escape(TITLE_PREFIX) + r"\s*\[(\d+)\]", window_title)
    return int(m.group(1)) if m else 0


def parse_title(window_title: str) -> dict:
    """解析夹具标题：`CupDraw E2E Target [n] tool=.. panel=.. sliderClicks=.. ...`。

    标题里 `[n]` 仍是落笔总数（strokes_of 依赖该前缀格式），其后是空格分隔的
    键值对；`recent` 为逗号分隔的最近 8 笔工具名。
    """
    data: dict = {"strokes": strokes_of(window_title), "recent": []}
    m = re.search(re.escape(TITLE_PREFIX) + r"\s*\[(\d+)\]\s*(.*)$", window_title)
    if not m:
        return data
    for part in m.group(2).split():
        key, _, value = part.partition("=")
        if key:
            data[key] = value
    data["recent"] = [x for x in str(data.get("recent", "")).split(",") if x]
    for key in ("panel", "sliderClicks", "foldClicks", "drawPhaseSwitch"):
        try:
            data[key] = int(data.get(key, 0))
        except (TypeError, ValueError):
            data[key] = 0
    return data


def find_window(c: httpx.Client) -> tuple[int, str] | None:
    for w in c.get("/devices").json()["pcWindows"]:
        if w["title"].startswith(TITLE_PREFIX):
            return int(w["hwnd"]), w["title"]
    return None


# ---------- 主流程 ----------

def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cupdraw-e2e-"))
    chrome: subprocess.Popen | None = None
    server: socketserver.TCPServer | None = None
    c = client()
    try:
        # 0. 清理上次运行可能残留的目标窗口（按标题关闭，不动用户其他窗口）
        for stale in [w for w in c.get("/devices").json()["pcWindows"]
                      if w["title"].startswith(TITLE_PREFIX)]:
            user32.PostMessageW(ctypes.c_void_p(stale["hwnd"]), 0x0010, 0, 0)  # WM_CLOSE
        wait_for(lambda: find_window(c) is None, 8, "残留窗口关闭")

        # 1. 起目标窗口
        server, port = start_canvas_server()
        chrome = launch_chrome(port, tmp / "profile")
        hwnd, title = wait_for(lambda: find_window(c), 30, "目标窗口出现")
        check("目标窗口启动", hwnd > 0, f"hwnd={hwnd} title={title!r}")

        # 2. 注册为镜像目标（与真机登记外部 scrcpy 窗口同一接口）
        reg = post(c, "/devices/mirror/register",
                   json={"hwnd": hwnd, "serial": "e2e-virtual", "label": "E2E 虚拟投屏"}).json()
        check("注册镜像目标", reg.get("running") is True and reg.get("hwnd") == hwnd)

        # 3. 项目 + 校准（auto 检测白卡，deviceKind=mirror）
        source = tmp / "source.png"
        make_source_png(source)
        with source.open("rb") as f:
            project = post(c, "/projects",
                           files={"file": ("source.png", f, "image/png")},
                           data={"name": "E2E 无手机链路"}).json()
        project_id = project["projectId"]
        cal = post(c, f"/projects/{project_id}/calibration",
                   json={"hwnd": hwnd, "mode": "auto", "deviceKind": "mirror",
                         "deviceSerial": "e2e-virtual"}).json()
        check("画布自动校准", cal.get("calibrationId") is not None,
              f"rect={cal.get('rect')} img={cal.get('imageWidth')}x{cal.get('imageHeight')}")
        if cal.get("calibrationId") is None:
            return finish(c, chrome, server)

        # 4. 规划
        plan = post(c, f"/projects/{project_id}/plans", json={"params": {}}).json()
        plan_id = plan["planId"]
        total = int(plan.get("metrics", {}).get("pathCount") or 0)
        check("生成规划", total > 0, f"plan={plan_id} pathCount={total}")

        exec_params = {"eventStepPx": 2.0, "pointDelayMs": 3.0, "strokeDelayMs": 30.0,
                       "focusGuard": True, "guardPollMs": 50.0}

        # 5. 先画过至少 2 笔（确保是真·中途断点），再抢走前台 → 守护应亚秒内停笔
        task = post(c, f"/plans/{plan_id}/tasks",
                    json={"exec": exec_params, "countdownMs": 1200, "allowDrift": 0.1}).json()
        task_id = task["taskId"]
        print("… 探测中（首绘前自动识别工具条，每个候选点都要截图比对，需要 1-2 分钟）")
        wait_for(
            lambda: (s if (s := task_status(c, task_id))["cursorPath"] >= 2 else None),
            180, "已画过 2 笔（真中途断点）", interval_s=0.05,
        )
        t_min = time.monotonic()
        user32.ShowWindow(ctypes.c_void_p(hwnd), SW_MINIMIZE)  # 模拟用户最小化投屏窗口
        snap = wait_for(
            lambda: (s if (s := task_status(c, task_id))["status"] == "STOPPED" else None),
            10, "守护停笔", interval_s=0.05,
        )
        latency = time.monotonic() - t_min
        check("守护：最小化即停笔", snap["stopReason"] == "focus_lost",
              f"stopReason={snap['stopReason']} 停笔延迟={latency * 1000:.0f}ms")
        check("守护：停笔延迟 ≤ 500ms", latency <= 0.5, f"{latency * 1000:.0f}ms")
        mid_cursor = int(snap["cursorPath"])
        found = find_window(c)
        strokes_mid = strokes_of(found[1]) if found else 0

        # 6. 断点续画：恢复窗口 → resume → DONE
        user32.ShowWindow(ctypes.c_void_p(hwnd), SW_RESTORE)
        time.sleep(0.4)
        post(c, f"/tasks/{task_id}/resume", json={"countdownMs": 1200, "allowDrift": 0.1})
        snap = wait_for(
            lambda: (s if (s := task_status(c, task_id))["status"] in ("DONE", "FAILED", "STOPPED") else None),
            300, "续画到终态", interval_s=0.1,
        )
        check("断点续画完成", snap["status"] == "DONE" and snap["cursorPath"] == snap["totalPaths"],
              f"status={snap['status']} cursor={snap['cursorPath']}/{snap['totalPaths']} 断点={mid_cursor}")

        # 7. 注入真实落墨：画布笔画计数随绘制增长
        found = find_window(c)
        strokes_end = strokes_of(found[1]) if found else 0
        check("注入落墨可见（笔画计数增长）",
              strokes_end > 0 and strokes_end - strokes_mid >= 2,
              f"mid={strokes_mid} end={strokes_end} 追加={strokes_end - strokes_mid}（期望 ≥2）")

        # 8. 逐段笔宽：绘制全程画笔、面板收起、换宽次数与面板展开次数有界
        kv = parse_title(found[1]) if found else {"recent": []}
        total_paths = int(snap["totalPaths"])
        recent = kv.get("recent", [])
        # 夹具标题只保留最近 8 笔工具名；路径数超过 8 时只能核对最近 8 笔。
        tail_n = min(total_paths, 8)
        draw_tail = recent[-tail_n:] if tail_n else []
        print(f"… 夹具计数：{found[1] if found else '<窗口已关闭>'}")
        check("绘制期全程画笔（最近各笔均为 brush）",
              len(draw_tail) == tail_n and all(t == "brush" for t in draw_tail),
              f"totalPaths={total_paths} 核对最近 {tail_n} 笔 recent={recent}")
        check("终态工具为画笔（橡皮开局已被纠正）", kv.get("tool") == "brush",
              f"tool={kv.get('tool')}")
        check("绘制结束面板收起", kv.get("panel") == 0, f"panel={kv.get('panel')}")
        switches = int(kv.get("drawPhaseSwitch", 0))
        switch_limit = math.ceil(1 / 0.05) + 1
        check("换宽确实发生且有界", 2 <= switches <= switch_limit,
              f"drawPhaseSwitch={switches} 区间=[2,{switch_limit}]")
        width_table = (snap.get("exec") or {}).get("widthTable") or []
        check("标定档位 ≥ 2（仅证明标定成功；桶数由离线验证覆盖）", len(width_table) >= 2,
              f"widthTable={width_table}")
        fold_clicks = int(kv.get("foldClicks", 0))
        check("面板开合次数在合理区间", 2 <= fold_clicks <= 21,
              f"foldClicks={fold_clicks} 区间=[2,21]")

        # 9. 注销后句柄失效判定
        c.request("DELETE", "/devices/mirror", headers={"X-Requested-With": "cupdraw"})
        status = c.get("/devices").json()["mirror"]
        check("注销镜像目标", status.get("running") is False)
    except TimeoutError as exc:
        check("流程超时", False, str(exc))
    except Exception as exc:  # noqa: BLE001 —— E2E 兜底报告
        check("流程异常", False, repr(exc))
    finally:
        if chrome is not None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(chrome.pid)],
                           capture_output=True)
        if server is not None:
            server.shutdown()
            server.server_close()
    return finish(c, chrome, server)


def finish(c: httpx.Client, chrome, server) -> int:
    try:
        c.request("DELETE", "/devices/mirror", headers={"X-Requested-With": "cupdraw"})
    except Exception:  # noqa: BLE001
        pass
    print("\n== 结果 ==")
    if FAILED:
        print(f"FAILED x{len(FAILED)}: {'; '.join(FAILED)}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
