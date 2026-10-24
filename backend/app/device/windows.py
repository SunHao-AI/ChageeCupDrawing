"""桌面窗口枚举与存活性查询。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    class_name: str


def _hwnd_value(raw: object) -> int:
    value = getattr(raw, "value", raw)
    return int(value or 0)


def list_windows() -> list[WindowInfo]:
    from maa.toolkit import Toolkit

    windows: list[WindowInfo] = []
    for item in Toolkit.find_desktop_windows():
        hwnd = _hwnd_value(item.hwnd)
        if not hwnd:
            continue
        windows.append(
            WindowInfo(hwnd=hwnd, title=str(item.window_name), class_name=str(item.class_name))
        )
    return windows


def window_alive(hwnd: int) -> bool:
    _user32.IsWindow.argtypes = [ctypes.c_void_p]
    _user32.IsWindow.restype = wintypes.BOOL
    return bool(_user32.IsWindow(ctypes.c_void_p(int(hwnd))))


def foreground_hwnd() -> int:
    _user32.GetForegroundWindow.argtypes = []
    _user32.GetForegroundWindow.restype = ctypes.c_void_p
    return _hwnd_value(_user32.GetForegroundWindow())


def root_owner(hwnd: int) -> int:
    """沿 GA_ROOTOWNER 上溯顶层属主。微信小程序的运行期前台句柄可能是内嵌子
    窗口，直接与顶层句柄比较会误判失焦，双方都归一到属主再比。"""
    GA_ROOTOWNER = 3
    _user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    _user32.GetAncestor.restype = ctypes.c_void_p
    return _hwnd_value(_user32.GetAncestor(ctypes.c_void_p(int(hwnd)), GA_ROOTOWNER)) or int(hwnd)


def foreground_matches(hwnd: int) -> bool:
    """目标窗口是否拥有前台（按顶层属主比较，兼容内嵌子窗口场景）。"""
    fg = foreground_hwnd()
    return fg != 0 and (fg == int(hwnd) or root_owner(fg) == root_owner(int(hwnd)))


def window_usable(hwnd: int) -> bool:
    """窗口存在、可见且未最小化；最小化与隐藏可恢复，按暂停而非失败处理。"""
    _user32.IsWindow.argtypes = [ctypes.c_void_p]
    _user32.IsWindow.restype = wintypes.BOOL
    if not _user32.IsWindow(ctypes.c_void_p(int(hwnd))):
        return False
    _user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    _user32.IsWindowVisible.restype = wintypes.BOOL
    if not _user32.IsWindowVisible(ctypes.c_void_p(int(hwnd))):
        return False
    _user32.IsIconic.argtypes = [ctypes.c_void_p]
    _user32.IsIconic.restype = wintypes.BOOL
    return not _user32.IsIconic(ctypes.c_void_p(int(hwnd)))


def window_title(hwnd: int) -> str:
    _user32.GetWindowTextW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    buffer = ctypes.create_unicode_buffer(512)
    _user32.GetWindowTextW(ctypes.c_void_p(int(hwnd)), buffer, len(buffer))
    return buffer.value


def activate_window(hwnd: int) -> bool:
    """把目标窗口带到前台（尽力而为）。

    Windows 前台锁只允许当前前台进程改前台，直接 SetForegroundWindow 常被忽略；
    经典绕法是把自己的输入线程附加到前台线程再调用。恢复最小化后一并处理。
    """
    target = int(hwnd)
    if not window_alive(target):
        return False
    _user32.IsIconic.argtypes = [ctypes.c_void_p]
    _user32.IsIconic.restype = wintypes.BOOL
    if _user32.IsIconic(ctypes.c_void_p(target)):
        SW_RESTORE = 9
        _user32.ShowWindow(ctypes.c_void_p(target), SW_RESTORE)

    fg = foreground_hwnd()
    _user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    target_tid = _user32.GetWindowThreadProcessId(ctypes.c_void_p(target), None)
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    current_tid = _kernel32.GetCurrentThreadId()
    attached = False
    if fg and target_tid and target_tid != current_tid:
        _user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        _user32.AttachThreadInput.restype = wintypes.BOOL
        attached = bool(_user32.AttachThreadInput(ctypes.c_ulong(target_tid), ctypes.c_ulong(current_tid), True))
    try:
        _user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
        _user32.BringWindowToTop.restype = wintypes.BOOL
        _user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        _user32.SetForegroundWindow.restype = wintypes.BOOL
        _user32.SetActiveWindow.argtypes = [ctypes.c_void_p]
        _user32.SetActiveWindow.restype = ctypes.c_void_p
        _user32.BringWindowToTop(ctypes.c_void_p(target))
        _user32.SetForegroundWindow(ctypes.c_void_p(target))
        if attached:
            # 附加期间把窗口激活一并转交，成功率更高。
            _user32.SetActiveWindow(ctypes.c_void_p(target))
        # 以实际前台归属判定成功：SetForegroundWindow 被前台锁拒绝时只闪任务栏。
        return foreground_matches(target)
    finally:
        if attached:
            _user32.AttachThreadInput(ctypes.c_ulong(target_tid), ctypes.c_ulong(current_tid), False)
