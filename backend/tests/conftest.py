"""金标准回归所需的夹具：加载 V6 单文件实现、样例图片与确定性派生样本。"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Iterator

import pytest
from PIL import Image, ImageEnhance, ImageOps

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
EXAMPLES_DIR = REPO_ROOT / "data" / "examples"

V6_PATH = Path(
    os.environ.get(
        "CUPDRAW_V6_PATH",
        str(Path("e:/Workplace/CupDrawing-spike/cup_drawbot.py")),
    )
)

# 每个样例派生 40 个确定性变体，5 张真实图片合计 200 个样本。
VARIANTS_PER_IMAGE = 40

IMAGE_MODES = ("fidelity", "adaptive", "edge", "dither", "threshold")
IMAGE_MODES_V6 = {
    "fidelity": "原图保真（推荐）",
    "adaptive": "自适应文字/手绘",
    "edge": "边缘线稿",
    "dither": "黑白抖动",
    "threshold": "阈值黑白",
}

STRATEGIES = ("topology", "centerline", "contour", "hscan")
STRATEGIES_V6 = {
    "topology": "拓扑保真填充（推荐）",
    "centerline": "中心线（线稿）",
    "contour": "轮廓（空心）",
    "hscan": "水平填充（兼容）",
}

FIT_MODES = ("contain", "cover")
FIT_MODES_V6 = {"contain": "完整显示（留白）", "cover": "铺满画布（裁剪）"}

CANVAS_SIZES = ((180, 240), (220, 300), (260, 200))
BRUSH_WIDTHS = (2, 3, 4)
THRESHOLDS = (205, 180, 225, 160)
SIMPLIFY_PX = (0.16, 0.24, 0.10)
SAFE_MARGINS = (3, 4, 6)
FILL_DENSITY = (0.82, 0.70, 0.95)
DETAIL_KEEP = (0.14, 0.22)
SUPERSAMPLE = (1, 2)
LINE_EXPAND = (0, 1, -1)
CONTRAST = (1.0, 1.15)
SCALES = (0.30, 0.45, 0.60, 0.80, 1.00)
ROTATIONS = (0, 6, -8, 90, 180, 0, 12, -15)
JPEG_QUALITY = (None, 55, 75, 92, None, 88, 60, None)
EXTRA_CONTRAST = (1.0, 0.85, 1.25, 1.0, 1.1, 0.95, 1.2, 1.0)


def load_v6():
    if not V6_PATH.is_file():
        pytest.skip(f"未找到 V6 参考实现：{V6_PATH}（可通过 CUPDRAW_V6_PATH 覆盖）")
    spec = importlib.util.spec_from_file_location("cup_drawbot_v6", V6_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def v6():
    return load_v6()


@pytest.fixture(scope="session")
def example_images() -> list[Path]:
    paths = sorted(EXAMPLES_DIR.glob("*.png"))
    if not paths:
        pytest.skip(f"未找到样例图片：{EXAMPLES_DIR}")
    return paths


def load_grayscale(path: Path) -> Image.Image:
    img = ImageOps.exif_transpose(Image.open(path))
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, "white")
        bg.alpha_composite(rgba)
        img = bg.convert("RGB")
    return img.convert("L")


def derive_variant(base: Image.Image, index: int) -> Image.Image:
    """由真实样例确定性派生变体，覆盖缩放/旋转/翻转/JPEG 往返/对比度。"""
    img = base
    scale = SCALES[index % len(SCALES)]
    if scale != 1.0:
        size = (max(8, round(img.width * scale)), max(8, round(img.height * scale)))
        img = img.resize(size, Image.Resampling.BICUBIC)

    rotation = ROTATIONS[(index // len(SCALES)) % len(ROTATIONS)]
    if rotation:
        img = img.rotate(rotation, expand=True, fillcolor=255)

    if index % 7 == 3:
        img = ImageOps.mirror(img)

    quality = JPEG_QUALITY[(index // len(SCALES)) % len(JPEG_QUALITY)]
    if quality is not None:
        import io

        buf = io.BytesIO()
        img.convert("L").save(buf, format="JPEG", quality=quality)
        buf.seek(0)
        img = Image.open(buf).convert("L")

    extra = EXTRA_CONTRAST[(index // len(SCALES)) % len(EXTRA_CONTRAST)]
    if extra != 1.0:
        img = ImageEnhance.Contrast(img).enhance(extra)
    return img.convert("L")


def plan_params(index: int) -> dict:
    """为第 index 个样本生成一组确定性参数，覆盖策略/模式/数值维度。"""
    return {
        "image_mode": IMAGE_MODES[(index // len(STRATEGIES)) % len(IMAGE_MODES)],
        "strategy": STRATEGIES[index % len(STRATEGIES)],
        "threshold": THRESHOLDS[index % len(THRESHOLDS)],
        "brush_width": BRUSH_WIDTHS[index % len(BRUSH_WIDTHS)],
        "simplify_px": SIMPLIFY_PX[index % len(SIMPLIFY_PX)],
        "safe_margin": SAFE_MARGINS[index % len(SAFE_MARGINS)],
        "fill_density": FILL_DENSITY[index % len(FILL_DENSITY)],
        "supersample": SUPERSAMPLE[index % len(SUPERSAMPLE)],
        "detail_keep": DETAIL_KEEP[index % len(DETAIL_KEEP)],
        "line_expand": LINE_EXPAND[index % len(LINE_EXPAND)],
        "contrast": CONTRAST[index % len(CONTRAST)],
        "fit_mode": FIT_MODES[index % len(FIT_MODES)],
        "thin_finish": index % 3 != 2,
        "auto_trim": index % 5 != 4,
        "topology_rescue": True,
        "target_recall": 0.985,
        "legacy_row_step": 3,
        "canvas": CANVAS_SIZES[index % len(CANVAS_SIZES)],
    }


def iter_samples(images: list[Path]) -> Iterator[tuple[str, Image.Image, dict]]:
    total_index = 0
    for path in images:
        base = load_grayscale(path)
        for v in range(VARIANTS_PER_IMAGE):
            yield f"{path.stem}-v{v:02d}", derive_variant(base, v), plan_params(total_index)
            total_index += 1
