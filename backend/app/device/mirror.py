"""镜像目标（B2）：把手机画面投屏成 PC 窗口，注入与守护完全复用 PC 窗口链路。

设计依据 docs/focus-guard-and-multidevice-design.md §4.5：
- 首选 scrcpy 投屏窗口。目标在 PC 侧仍是一个窗口，因此截图（MaaFramework
  Win32 控制器走 Windows.Graphics.Capture，能抓到 GPU 合成的 SDL 画面）、
  SendInput 注入、焦点/光标/左键守护、坐标换算全部零改动继承。
- 注入通路后续可切 adb shell（MaaTouch）以降低时延，届时只需替换注入实现，
  DrawingTarget 接口不变。
- 本模块不负责「找到投屏窗口」以外的事情；投屏窗口关闭即视为设备丢失。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from app.device.service import DeviceService

REGISTRY_NAME = "mirror.json"
WINDOW_TITLE_PREFIX = "CupDraw Mirror"
# scrcpy 冷启动（含 adb 建连）通常 <2 s，给足余量再判定失败。
LAUNCH_WAIT_S = 8.0


class MirrorError(Exception):
    """设备层失败，由路由层转换为统一错误体。"""

    def __init__(self, status: int, code: str, message: str, detail: object = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail


@dataclass
class MirrorStatus:
    """当前镜像目标状态；serial 为 None 表示尚未注册任何投屏目标。"""

    serial: str | None = None
    label: str = ""
    hwnd: int = 0
    running: bool = False
    window_title: str = ""

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class MirrorService:
    """镜像目标注册表：记录 serial ↔ 投屏窗口句柄，句柄失效即判设备丢失。"""

    def __init__(self, data_dir: Path, device: DeviceService) -> None:
        self._path = Path(data_dir) / REGISTRY_NAME
        self._device = device
        self._process: subprocess.Popen[bytes] | None = None

    # ---------- 注册表 ----------

    def _read(self) -> dict:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def status(self) -> MirrorStatus:
        data = self._read()
        hwnd = int(data.get("hwnd") or 0)
        if not hwnd or not self._device.alive(hwnd):
            return MirrorStatus(
                serial=data.get("serial"),
                label=data.get("label") or data.get("serial") or "",
            )
        return MirrorStatus(
            serial=data.get("serial"),
            label=data.get("label") or data.get("serial") or "",
            hwnd=hwnd,
            running=True,
            window_title=self._device.title(hwnd),
        )

    def register_window(self, *, hwnd: int, serial: str, label: str = "") -> MirrorStatus:
        """把一个已存在的窗口登记为镜像目标。

        真机场景用于登记用户手工启动的 scrcpy；调试场景允许登记任意窗口，
        以便在无手机环境下验证「目标注册 → 校准 → 绘制 → 守护」全链路。
        """
        if not self._device.alive(hwnd):
            raise MirrorError(409, "E_WINDOW_LOST", "目标窗口已关闭或句柄失效")
        if not serial.strip():
            raise MirrorError(400, "E_INVALID_ARGUMENT", "serial 不能为空")
        self._write({
            "serial": serial.strip(),
            "label": (label.strip() or serial.strip())[:64],
            "hwnd": int(hwnd),
            "registeredAt": time.time(),
        })
        return self.status()

    def clear(self) -> None:
        self._path.unlink(missing_ok=True)

    # ---------- scrcpy 启动 ----------

    @staticmethod
    def tool_path(name: str) -> str | None:
        """定位 adb / scrcpy：环境变量覆盖优先，其次 PATH。"""
        return os.environ.get(f"CUPDRAW_{name.upper()}") or shutil.which(name)

    def tools_available(self) -> tuple[bool, list[str]]:
        missing = [name for name in ("adb", "scrcpy") if not self.tool_path(name)]
        return not missing, missing

    def devices(self) -> list[dict[str, str]]:
        """adb devices -l 的序列号列表；工具缺失或未授权时返回空。"""
        adb = self.tool_path("adb")
        if not adb:
            return []
        try:
            out = subprocess.run(
                [adb, "devices", "-l"], capture_output=True, text=True, timeout=6
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        found: list[dict[str, str]] = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                model = next(
                    (p.split(":", 1)[1] for p in parts[2:] if p.startswith("model:")), ""
                )
                found.append({"serial": parts[0], "model": model.replace("_", " ")})
        return found

    def launch(self, serial: str) -> MirrorStatus:
        """启动 scrcpy 投屏并登记其窗口；工具缺失或窗口未出现时抛 TaskError。"""
        available, missing = self.tools_available()
        if not available:
            raise MirrorError(
                503,
                "E_SCRCPY_MISSING",
                f"缺少投屏工具：{'、'.join(missing)}。可安装后重试，"
                "或用「登记已有窗口」把手工启动的 scrcpy 窗口接入",
            )
        if not any(d["serial"] == serial for d in self.devices()):
            raise MirrorError(409, "E_DEVICE_NOT_FOUND", f"未找到已授权设备 {serial}")

        title = f"{WINDOW_TITLE_PREFIX} - {serial}"
        scrcpy, adb = self.tool_path("scrcpy"), self.tool_path("adb")
        try:
            self._process = subprocess.Popen(
                [scrcpy, "--serial", serial, "--window-title", title,
                 "--no-audio", "--stay-awake"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={**os.environ, "ADB": adb or "adb"},
            )
        except OSError as exc:
            raise MirrorError(503, "E_SCRCPY_MISSING", "scrcpy 启动失败", str(exc)) from exc

        deadline = time.monotonic() + LAUNCH_WAIT_S
        while time.monotonic() < deadline:
            hwnd = self._find_window_by_title(title)
            if hwnd:
                self._write({
                    "serial": serial, "label": title, "hwnd": hwnd,
                    "registeredAt": time.time(),
                })
                return self.status()
            if self._process.poll() is not None:
                break
            time.sleep(0.2)

        self.stop_process()
        raise MirrorError(503, "E_MIRROR_WINDOW_MISSING", "scrcpy 投屏窗口未出现", title)

    def stop_process(self) -> None:
        """结束本进程启动的 scrcpy；外部启动的只清登记，不动用户窗口。"""
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
        self._process = None

    def _find_window_by_title(self, title: str) -> int:
        for window in self._device.list_windows():
            if window.title == title:
                return window.hwnd
        return 0
