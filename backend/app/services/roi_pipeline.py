"""生成通道 ROI 管线：裁剪 → letterbox → 模型编辑 → 反算 → 羽化回填。

杯贴画布可达 5194×698，云端模型只接受常规比例的小图，整图提交必被缩放压糊。
这里把「编辑哪块、怎么缩放、怎么贴回」收敛为一个确定性流程（设计文档
ai-edit-provider-design §4.1）：

1. 有 roi 用 roi 外扩取矩形；无 roi 时宽画布按内容外接框裁剪，四周留白不动。
2. 裁块等比缩放到模型档位并居中补齐为正方形（letterbox），补齐参数全部记录。
3. 模型输出（可能是固定尺寸方图）按 letterbox 参数反算裁回、拉齐裁块原尺寸。
4. 结果二值化为墨迹，用边缘 alpha 渐变与底图融合，避免接缝在二值化后长出新线。

管线只负责几何与融合，具体的 HTTP 调用由 provider 通过 `edit_fn` 注入，便于
用假响应做契约测试。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

# 模型档位：长边缩放到该值后补齐为正方形，覆盖主流图像编辑模型的输入尺寸档。
MODEL_EDGE = 1024
# 内容外接框判定阈值：接近纯白的像素不算内容。
CONTENT_THRESHOLD = 247
CONTENT_PAD_RATIO = 0.02
# 选区外扩比例：给模型留出上下文，避免沿选区边界生硬切割。
ROI_CONTEXT_RATIO = 0.12
# 宽画布（长边或长宽比越界）才按内容外接框裁剪，普通图片保持整图编辑。
WIDE_EDGE = 2048
WIDE_RATIO = 3.0
# 接缝过渡带（像素），落在 [8, 16] 区间。
FEATHER_MIN = 8
FEATHER_MAX = 16
# 模型输出的线稿可能带抗锯齿灰边，二值化阈值略高于 load_ink 的 128。
INK_THRESHOLD = 160
# 裁块过小时不无限放大，最多 4 倍，剩余补齐交给 letterbox。
MAX_UPSCALE = 4.0

PROMPT_TEMPLATE = """你是线稿修图助手。按下面的指令修改图片：
{instruction}

必须遵守：
1. 只改指令涉及的内容，其余构图、位置、线条形状保持不变；
2. 输出与输入同尺寸，纯白背景 + 纯黑色线稿或素描笔触，不要出现彩色、渐变、
   灰底、纹理、额外边框或水印；
3. 若指令要求添加文字，使用清晰、笔画完整的黑色实心字，字号与画面协调。"""

EditFn = Callable[[Image.Image, str], Image.Image]


@dataclass(frozen=True)
class Letterbox:
    """letterbox 参数：缩放到 (rw, rh) 后居中贴在 (edge, edge) 白底上。"""

    edge: int
    rw: int
    rh: int
    pad_x: int
    pad_y: int


def build_prompt(instruction: str) -> str:
    return PROMPT_TEMPLATE.format(instruction=instruction.strip())


def _content_rect(gray: np.ndarray) -> tuple[int, int, int, int] | None:
    ys, xs = np.where(gray < CONTENT_THRESHOLD)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def _expand(rect: tuple[int, int, int, int], pad: int, size: tuple[int, int]) -> tuple[int, int, int, int]:
    width, height = size
    x0, y0, x1, y1 = rect
    return max(0, x0 - pad), max(0, y0 - pad), min(width, x1 + pad), min(height, y1 + pad)


def _needs_content_crop(size: tuple[int, int]) -> bool:
    width, height = size
    return max(width, height) > WIDE_EDGE or max(width / height, height / width) > WIDE_RATIO


def choose_rect(size: tuple[int, int], roi: dict | None) -> tuple[int, int, int, int]:
    """选区外扩取矩形（像素坐标，右开区间）；无 roi 时返回整图。"""
    width, height = size
    if roi is None:
        return 0, 0, width, height
    x0 = max(0, min(width - 1, int(roi["x"])))
    y0 = max(0, min(height - 1, int(roi["y"])))
    x1 = max(x0 + 1, min(width, x0 + max(1, int(roi["w"]))))
    y1 = max(y0 + 1, min(height, y0 + max(1, int(roi["h"]))))
    pad = int(round(max(x1 - x0, y1 - y0) * ROI_CONTEXT_RATIO))
    if pad > 0:
        x0, y0, x1, y1 = _expand((x0, y0, x1, y1), pad, size)
    return x0, y0, x1, y1


def letterbox(image: Image.Image, edge: int = MODEL_EDGE) -> tuple[Image.Image, Letterbox]:
    width, height = image.size
    scale = min(float(edge) / float(max(width, height)), MAX_UPSCALE)
    rw = max(1, min(edge, int(round(width * scale))))
    rh = max(1, min(edge, int(round(height * scale))))
    resized = image.resize((rw, rh), Image.LANCZOS)
    canvas = Image.new("L", (edge, edge), 255)
    pad_x = (edge - rw) // 2
    pad_y = (edge - rh) // 2
    canvas.paste(resized, (pad_x, pad_y))
    return canvas, Letterbox(edge=edge, rw=rw, rh=rh, pad_x=pad_x, pad_y=pad_y)


def unletterbox(image: Image.Image, meta: Letterbox, size: tuple[int, int]) -> Image.Image:
    """按 letterbox 参数反算裁回，并拉齐到原裁块尺寸。

    模型返回的尺寸有两种常见形态：
    - 与裁块同比例（Gemini 等）：直接缩放到裁块尺寸；
    - 固定正方形（gpt-image 系列）：按 letterbox 参数裁掉补齐区再缩放。
    """
    crop_ratio = size[0] / size[1]
    if abs(image.width / image.height - crop_ratio) <= 0.02:
        return image.resize(size, Image.LANCZOS)
    if image.size != (meta.edge, meta.edge):
        image = image.resize((meta.edge, meta.edge), Image.LANCZOS)
    cropped = image.crop((meta.pad_x, meta.pad_y, meta.pad_x + meta.rw, meta.pad_y + meta.rh))
    return cropped.resize(size, Image.LANCZOS)


def feather_alpha(shape: tuple[int, int]) -> np.ndarray:
    """矩形边缘线性渐变的 alpha：内部 1、接缝处趋近 0（四角为两边乘积）。"""
    height, width = shape
    band = min(max(FEATHER_MIN, min(width, height) // 8), FEATHER_MAX)
    band = min(band, max(1, min(width, height) // 2))
    ramp = np.linspace(0.0, 1.0, band + 1, dtype=np.float32)[1:]
    alpha = np.ones((height, width), dtype=np.float32)
    alpha[:band, :] *= ramp[:, None]
    alpha[-band:, :] *= ramp[::-1][:, None]
    alpha[:, :band] *= ramp[None, :]
    alpha[:, -band:] *= ramp[::-1][None, :]
    return alpha


def _to_ink(image: Image.Image) -> np.ndarray:
    gray = np.asarray(image.convert("L"), dtype=np.uint8)
    return ((gray < INK_THRESHOLD).astype(np.uint8)) * 255


def blend_ink(base: np.ndarray, patch: np.ndarray, rect: tuple[int, int, int, int]) -> np.ndarray:
    """把裁块墨迹按羽化 alpha 贴回底图，返回新的二值墨迹数组。"""
    x0, y0, x1, y1 = rect
    target = base[y0:y1, x0:x1]
    if patch.shape != target.shape:
        patch = cv2.resize(patch, (target.shape[1], target.shape[0]), interpolation=cv2.INTER_NEAREST)
    alpha = feather_alpha(target.shape)
    mixed = target.astype(np.float32) * (1.0 - alpha) + patch.astype(np.float32) * alpha
    out = base.copy()
    out[y0:y1, x0:x1] = np.where(mixed >= 127.5, 255, 0).astype(np.uint8)
    return out


def generative_edit(
    source: Path,
    roi: dict | None,
    instruction: str,
    out_path: Path,
    edit_fn: EditFn,
) -> None:
    """执行一次生成编辑并写出白底黑墨草稿。edit_fn 负责真实模型调用。"""
    gray = np.asarray(Image.open(source).convert("L"), dtype=np.uint8)
    height, width = gray.shape
    x0, y0, x1, y1 = _pick_rect(gray, roi)
    crop = Image.fromarray(gray[y0:y1, x0:x1], mode="L")
    canvas, meta = letterbox(crop)
    result = edit_fn(canvas, build_prompt(instruction))
    patch = unletterbox(result.convert("L"), meta, (x1 - x0, y1 - y0))
    base_ink = ((gray < 128).astype(np.uint8)) * 255
    ink = blend_ink(base_ink, _to_ink(patch), (x0, y0, x1, y1))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.where(ink > 0, 0, 255).astype(np.uint8), mode="L").save(out_path)


def _pick_rect(gray: np.ndarray, roi: dict | None) -> tuple[int, int, int, int]:
    height, width = gray.shape
    size = (width, height)
    if roi is not None:
        return choose_rect(size, roi)
    if not _needs_content_crop(size):
        return 0, 0, width, height
    content = _content_rect(gray)
    if content is None:
        return 0, 0, width, height
    pad = max(2, int(round(max(content[2] - content[0], content[3] - content[1]) * CONTENT_PAD_RATIO)))
    return _expand(content, pad, size)
