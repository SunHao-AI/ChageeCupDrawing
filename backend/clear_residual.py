"""清场脚本：连点撤销按钮直到画布墨量归零。

用于清掉历次失败 run 留下的同位残线。自动探测（ToolbarProber）在这种场景下
结构性失明：残线重叠导致带内墨量「纹丝不动」，撤销点击后图标只翻转一次，纯
墨量判据与反应复验判据都无法区分「撤销生效」与「点到别处」。本脚本不依赖
探测判据，直接动态定位撤销按钮、连点撤销、只看画布墨量的单调下降。

与 clear_canvas API 的区别：
  1. 撤销坐标动态定位（画布下方第一个内容行的最左图标），不依赖已入库坐标
  2. 不做 plateau 早停（残线重叠时墨量长期不动，plateau 会误判「撤到底」）
  3. 预算给足（默认 300 次），并打印每步墨量便于观察

用法：
  python clear_residual.py                 # 用最新校准的项目窗口
  python clear_residual.py --dry-run       # 只定位撤销按钮，不点击
  python clear_residual.py --undo-x 32 --undo-y 930 --max-clicks 300
"""

from __future__ import annotations

import argparse
import sys
import time

import numpy as np

from app.config import Settings
from app.core.vision.canvas_detect import detect_canvas_rect
from app.device.geometry import image_point_to_screen, make_dpi_aware
from app.device.service import DeviceService
from app.device.target import Win32Target
from app.executor.guard import Win32GuardProbe
from app.executor.ink_measure import ink_pixels
from app.executor.native_mouse import NativeMouse
from app.store.store import Store

# 撤销按钮相对画布底缘的 y 偏移（真机两轮实测：画布底缘下方 ~64px 处）。
UNDO_Y_OFFSET = 64
# 画布下方扫描带的搜索深度。
SCAN_DEPTH = 160
# 内容判定：相对行带背景的灰度差。
CONTENT_DELTA = 30
# 撤销图标最小块宽（滤掉单列噪点/阴影）。
MIN_BLOCK_W = 12


def _gray(image: np.ndarray) -> np.ndarray:
    return image[..., 0] if image.ndim == 3 else image


def _locate_undo(image: np.ndarray, canvas: tuple[int, int, int, int]) -> tuple[int, int] | None:
    """定位撤销按钮：画布下方第一个内容行里 x 最小内容块的中心。

    撤销+重做是两个相邻的弯箭头（撤销在左、重做在右），撤销恒在胶囊最左。
    画布左缘之下的第一行内容（画笔入口等）通常比撤销行更靠下、更靠右，
    因此「画布下方第一行内容 + 最左块」即撤销按钮。
    """
    gray = _gray(image).astype(np.int16)
    l, t, w, h = canvas
    bottom = t + h
    top = max(bottom, 0)
    bot = min(gray.shape[0], bottom + SCAN_DEPTH)
    if top >= bot:
        return None
    # 背景参考：画布下方空白区的中位灰度（避开左右两侧可能的控件）。
    bg = int(np.median(gray[top:bot, 200:400]))

    # 从画布底缘往下逐行找第一个有「撤销+重做」胶囊特征的内容行。
    # 撤销图标 x < 画布左缘 l（真机撤销恒探出画布左缘之外）。
    for y in range(top, bot):
        row = gray[y, :l]  # 只看画布左缘左侧
        delta = np.abs(row - bg)
        xs = np.flatnonzero(delta > CONTENT_DELTA)
        if xs.size < 3:
            continue
        blocks: list[list[int]] = []
        for x in xs.tolist():
            if blocks and x - blocks[-1][1] <= 3:
                blocks[-1][1] = x
            else:
                blocks.append([x, x])
        blocks = [b for b in blocks if b[1] - b[0] >= MIN_BLOCK_W]
        if not blocks:
            continue
        x0, x1 = blocks[0]
        return ((x0 + x1) // 2, y)
    return None


def _canvas_of(image: np.ndarray) -> tuple[int, int, int, int] | None:
    rect = detect_canvas_rect(image)
    if rect is None:
        rect = detect_canvas_rect(image, max_ratio=0.85)
    if rect is None:
        return None
    l, t, r, b = rect
    return (l, t, r - l, b - t)


def _latest_hwnd(store: Store) -> int | None:
    """从校准表取最近一次校准的目标窗口句柄。"""
    import sqlite3

    db = store.db_path
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT hwnd FROM canvas_profile ORDER BY captured_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return int(row[0]) if row else None


def _load_target(store: Store, device: DeviceService, hwnd: int | None) -> Win32Target:
    if hwnd is None:
        hwnd = _latest_hwnd(store)
    if hwnd is None:
        raise SystemExit("无任何校准记录且未指定 --hwnd，无法定位目标窗口")
    probe = Win32GuardProbe()
    target = Win32Target(hwnd, device, probe)
    if not target.alive():
        raise SystemExit(f"目标窗口已关闭（hwnd={hwnd}）")
    return target


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hwnd", type=int, default=None, help="目标窗口句柄（默认取最新校准）")
    ap.add_argument("--dry-run", action="store_true", help="只定位撤销按钮并打印，不点击")
    ap.add_argument("--undo-x", type=float, default=None, help="手动指定撤销按钮 x（图像坐标）")
    ap.add_argument("--undo-y", type=float, default=None, help="手动指定撤销按钮 y（图像坐标）")
    ap.add_argument("--max-clicks", type=int, default=300, help="撤销点击预算")
    ap.add_argument("--settle", type=float, default=0.22, help="每次点击后的沉降秒数")
    ap.add_argument("--plateau", type=int, default=0, help="连续 N 次墨量不动判「撤到底」，0 关闭")
    args = ap.parse_args()

    make_dpi_aware()
    settings = Settings()
    store = Store(settings.data_dir)
    store.init()
    device = DeviceService()
    target = _load_target(store, device, args.hwnd)
    mouse = NativeMouse()

    image = target.capture_image()
    if image is None:
        raise SystemExit("截图失败")
    canvas = _canvas_of(image)
    if canvas is None:
        raise SystemExit("未识别到画布矩形")

    if args.undo_x is not None and args.undo_y is not None:
        undo = (int(args.undo_x), int(args.undo_y))
        print(f"[清场] 使用手动撤销坐标 undo={undo}")
    else:
        undo = _locate_undo(image, canvas)
        if undo is None:
            raise SystemExit(
                "未定位到撤销按钮；可先用 --dry-run 截图核对，再 --undo-x/--undo-y 手动指定"
            )
        print(f"[清场] 画布 canvas={canvas}，撤销按钮 undo={undo}")

    region = (canvas[0], canvas[1], canvas[2], canvas[3])
    ink0 = ink_pixels(image, region)
    print(f"[清场] 初始画布墨量 ink={ink0} px")
    if args.dry_run:
        return 0
    if ink0 == 0:
        print("[清场] 画布已无墨，无需清场")
        return 0

    client = target.client_area()
    image_h, image_w = image.shape[:2]

    def click_undo() -> None:
        if not target.foreground_matches():
            target.activate()
            time.sleep(0.15)
        px, py = image_point_to_screen(undo[0], undo[1], image_w, image_h, client)
        mouse.click(px, py)

    ink = ink0
    plateau = 0
    for i in range(1, args.max_clicks + 1):
        click_undo()
        time.sleep(args.settle)
        image = target.capture_image()
        if image is None:
            print(f"[清场] 第 {i} 次点击后截图失败，停止")
            break
        now = ink_pixels(image, region)
        dropped = ink - now
        print(f"[清场] 第 {i:3d} 次：ink {ink} -> {now}（Δ{dropped:+d}）")
        if now == 0:
            print(f"[清场] 画布已清空，共点击 {i} 次")
            return 0
        # 墨量上升 = 点到重做/画笔，立即停止，避免越清越脏。
        if now > ink:
            print("[清场] 墨量上升，疑似点到重做或误触，立即停止")
            return 2
        ink = now
        if args.plateau > 0:
            plateau = plateau + 1 if abs(dropped) < 8 else 0
            if plateau >= args.plateau:
                print(f"[清场] 连续 {plateau} 次墨量不动，判定撤销已到顶")
                break

    print(f"[清场] 预算耗尽或已到顶，最终墨量 ink={ink} px（初始 {ink0} px）")
    return 0 if ink == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
