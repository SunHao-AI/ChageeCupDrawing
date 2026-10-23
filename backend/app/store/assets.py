"""资产落盘：上传原图规范化与项目/规划目录约定。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from app.config import MAX_IMAGE_LONG_SIDE

SUPPORTED_FORMATS = {"PNG": "png", "JPEG": "jpg"}


class UnsupportedMedia(Exception):
    """不是 PNG/JPEG 或文件已损坏。"""


class ImageTooLarge(Exception):
    """图像长边超过上限。"""


@dataclass(frozen=True)
class SavedImage:
    path: Path
    width: int
    height: int
    ext: str


def save_source_image(tmp_path: Path, project_dir: Path) -> SavedImage:
    """校验格式并用 Pillow 重新编码落盘，不信任原始扩展名。"""
    project_dir.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(tmp_path) as img:
            fmt = str(img.format or "").upper()
            if fmt not in SUPPORTED_FORMATS:
                raise UnsupportedMedia(f"不支持的图像格式：{fmt or 'unknown'}")
            img.load()
            width, height = img.size
            if max(width, height) > MAX_IMAGE_LONG_SIDE:
                raise ImageTooLarge(
                    f"图像长边 {max(width, height)} px 超过上限 {MAX_IMAGE_LONG_SIDE} px"
                )
            ext = SUPPORTED_FORMATS[fmt]
            target = project_dir / f"source_v1.{ext}"
            if fmt == "JPEG":
                img.convert("RGB").save(target, format="JPEG", quality=95)
            else:
                img.save(target, format="PNG")
    except (UnsupportedMedia, ImageTooLarge):
        raise
    except Exception as exc:  # Pillow 对损坏文件抛出多种异常
        raise UnsupportedMedia("图像文件无法解析") from exc

    return SavedImage(path=target, width=width, height=height, ext=ext)
