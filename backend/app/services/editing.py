"""编辑服务：EditProvider 协议、内置语义 provider 与 entry point 发现。

语义通道在「二值掩膜」上做仿射 / 形态学操作（设计稿 2.1）：掩膜由同一条
确定性二值化管线产出，前景即操作对象，变换结果与预览所见一致。解析未接
文本模型时用关键词规则兜底，保证不装任何扩展时语义通道开箱可用。

掩膜约定与 work_mask 相同：uint8，255=墨迹、0=空白；草稿 PNG 与 mask.png
同风格（白底黑墨）。
"""

from __future__ import annotations

import importlib.metadata
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "cupdraw.edit_providers"

_OP_TRANSLATE = "translate"
_OP_SCALE = "scale"
_OP_ROTATE = "rotate"
_OP_MIRROR = "mirror"
_OP_THICKEN = "thicken"
_OP_THIN = "thin"
_OP_CROP = "crop"
_OP_ERASE = "erase"

# 相对量默认步长（无显式像素数时取目标维度的 10%）。
_DEFAULT_STEP_RATIO = 0.10
_LIMIT_STEP_PX = 5000
_LIMIT_FACTOR = (0.1, 10.0)
_LIMIT_ANGLE = 360.0
_LIMIT_VALUE = 15


class EditError(Exception):
    """编辑失败的基类，由路由层翻译成 HTTP 错误。"""


class EditNotUnderstood(EditError):
    """指令无法解析为结构化操作，不做猜测性执行。"""


class UnknownProvider(EditError):
    """请求指定了未注册的 provider。"""


class NoGenerativeProvider(EditError):
    """生成通道无任何已注册 provider（Milestone 3 前恒为真）。"""


@dataclass(frozen=True)
class EditOp:
    op: str
    roi: dict | None = None
    dx: int | None = None
    dy: int | None = None
    factor: float | None = None
    angle: float | None = None
    value: int | None = None

    def as_dict(self) -> dict:
        payload: dict[str, object] = {"op": self.op}
        if self.roi is not None:
            payload["roi"] = self.roi
        if self.dx is not None:
            payload["dx"] = self.dx
        if self.dy is not None:
            payload["dy"] = self.dy
        if self.factor is not None:
            payload["factor"] = self.factor
        if self.angle is not None:
            payload["angle"] = self.angle
        if self.value is not None:
            payload["value"] = self.value
        return payload

    def with_roi(self, roi: dict | None) -> EditOp:
        if self.roi is not None or roi is None:
            return self
        return EditOp(
            op=self.op,
            roi=roi,
            dx=self.dx,
            dy=self.dy,
            factor=self.factor,
            angle=self.angle,
            value=self.value,
        )


class EditProvider(Protocol):
    id: str
    label: str
    supports: Literal["semantic", "generative", "both"]

    def parse(self, instruction: str) -> list[EditOp]: ...

    def apply(self, image: Path, ops: list[EditOp], out_path: Path) -> Path: ...

    def generate(
        self, image: Path, roi: dict | None, instruction: str, out_path: Path
    ) -> Path: ...


# ---------- 规则解析（兜底，无模型依赖） ----------

_TOKEN_SPLIT = re.compile(r"[，,。.;；、!！?？\n\r\t ]+")
_NUMBER = r"(-?\d+(?:\.\d+)?)"
_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(上移|下移|左移|右移)"), _OP_TRANSLATE),
    (re.compile(r"^(放大|缩小|缩放)"), _OP_SCALE),
    (re.compile(r"^旋转"), _OP_ROTATE),
    (re.compile(r"^(镜像|水平翻转|左右翻转|翻转)"), _OP_MIRROR),
    (re.compile(r"^(加粗|变粗|变细|减细)"), _OP_THICKEN),
    (re.compile(r"^(删除|去除|去掉|清除|擦除|擦掉|抹掉)"), _OP_ERASE),
    (re.compile(r"^(裁剪|裁切|只保留)"), _OP_CROP),
]

# 词-it 只是修饰语，单独出现不构成操作。
_FILLER = re.compile(r"^(请|帮我|帮忙|把|将|一下|一点|一些|图案|图形|区域|整体|全部|内容|吗|吧|了|的|图)$")


def _number_in(token: str) -> float | None:
    match = re.search(_NUMBER, token)
    return float(match.group(1)) if match else None


def _parse_op(token: str) -> EditOp | None:
    for pattern, op in _RULES:
        if not pattern.search(token):
            continue
        if op == _OP_TRANSLATE:
            return _translate_op(token)
        if op == _OP_SCALE:
            return _scale_op(token)
        if op == _OP_ROTATE:
            angle = _number_in(token)
            angle = 90.0 if angle is None else angle
            return EditOp(op=op, angle=max(-_LIMIT_ANGLE, min(_LIMIT_ANGLE, angle)))
        if op == _OP_MIRROR:
            return EditOp(op=op)
        if op == _OP_THICKEN:
            value = _number_in(token)
            value = 1 if value is None else abs(int(value))
            return EditOp(
                op="thin" if ("变细" in token or "减细" in token) else "thicken",
                value=max(1, min(_LIMIT_VALUE, value)),
            )
        if op in (_OP_ERASE, _OP_CROP):
            return EditOp(op=op)
    return None


def _translate_op(token: str) -> EditOp:
    raw = _number_in(token)
    amount: int | None = None
    if raw is not None and "%" not in token:
        amount = max(-_LIMIT_STEP_PX, min(_LIMIT_STEP_PX, int(raw)))
    if "上移" in token:
        return EditOp(op=_OP_TRANSLATE, dx=0, dy=None if amount is None else -abs(amount))
    if "下移" in token:
        return EditOp(op=_OP_TRANSLATE, dx=0, dy=abs(amount) if amount is not None else None)
    if "左移" in token:
        return EditOp(op=_OP_TRANSLATE, dy=0, dx=None if amount is None else -abs(amount))
    return EditOp(op=_OP_TRANSLATE, dy=0, dx=abs(amount) if amount is not None else None)


def _scale_op(token: str) -> EditOp:
    shrinking = "缩小" in token
    raw = _number_in(token)
    factor = 0.8 if shrinking else 1.2
    if raw is not None:
        if "倍" in token:
            factor = abs(raw) if raw != 0 else factor
        elif "%" in token:
            pct = abs(raw) / 100.0
            factor = (1.0 - pct) if shrinking else (1.0 + pct)
        elif raw > 0:
            factor = raw
    factor = max(_LIMIT_FACTOR[0], min(_LIMIT_FACTOR[1], factor))
    return EditOp(op=_OP_SCALE, factor=round(factor, 4))


def parse_instruction(instruction: str) -> list[EditOp]:
    ops: list[EditOp] = []
    for token in _TOKEN_SPLIT.split(instruction.strip()):
        if not token or _FILLER.match(token):
            continue
        op = _parse_op(token)
        if op is not None:
            ops.append(op)
    if not ops:
        raise EditNotUnderstood(f"无法从指令中解析出可执行操作：{instruction!r}")
    return ops


# ---------- OpenCV 执行 ----------

def load_ink(path: str | Path) -> np.ndarray:
    """读入源图/掩膜，返回 work_mask 约定（255=墨迹）的 uint8 数组。"""
    gray = np.asarray(Image.open(path).convert("L"), dtype=np.uint8)
    return ((gray < 128).astype(np.uint8)) * 255


def save_ink(ink: np.ndarray, out_path: Path) -> None:
    Image.fromarray(np.where(ink > 0, 0, 255).astype(np.uint8), mode="L").save(out_path)


def _region(shape: tuple[int, int], roi: dict | None) -> np.ndarray:
    height, width = shape
    mask = np.zeros((height, width), dtype=np.uint8)
    if roi is None:
        mask[:] = 255
        return mask
    x = max(0, int(roi["x"]))
    y = max(0, int(roi["y"]))
    x2 = min(width, x + max(1, int(roi["w"])))
    y2 = min(height, y + max(1, int(roi["h"])))
    mask[y:y2, x:x2] = 255
    return mask


def _inside(roi: dict | None, width: int, height: int) -> tuple[int, int, int, int]:
    if roi is None:
        return 0, 0, width, height
    x = max(0, min(width - 1, int(roi["x"])))
    y = max(0, min(height - 1, int(roi["y"])))
    x2 = max(x + 1, min(width, x + max(1, int(roi["w"]))))
    y2 = max(y + 1, min(height, y + max(1, int(roi["h"]))))
    return x, y, x2, y2


def _step(px: int | None, span: int) -> int:
    if px is not None:
        return px
    return int(round(span * _DEFAULT_STEP_RATIO))


def _warp(ink: np.ndarray, matrix: np.ndarray, region: np.ndarray | None) -> np.ndarray:
    height, width = ink.shape
    warped = cv2.warpAffine(
        ink,
        matrix,
        (width, height),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    if region is None:
        return warped
    return np.where(region > 0, warped, ink)


def _apply_translate(ink: np.ndarray, op: EditOp) -> np.ndarray:
    height, width = ink.shape
    x0, y0, x1, y1 = _inside(op.roi, width, height)
    dx = _step(op.dx, x1 - x0)
    dy = _step(op.dy, y1 - y0)
    region = _region(ink.shape, op.roi)
    matrix = np.float32([[1, 0, dx], [0, 1, dy]])
    return _warp(ink, matrix, region)


def _apply_scale(ink: np.ndarray, op: EditOp) -> np.ndarray:
    height, width = ink.shape
    x0, y0, x1, y1 = _inside(op.roi, width, height)
    center = ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
    matrix = cv2.getRotationMatrix2D(center, 0, float(op.factor or 1.0))
    return _warp(ink, matrix, _region(ink.shape, op.roi))


def _apply_rotate(ink: np.ndarray, op: EditOp) -> np.ndarray:
    height, width = ink.shape
    angle = float(op.angle or 0.0)
    if abs(angle) < 1e-3:
        return ink
    x0, y0, x1, y1 = _inside(op.roi, width, height)
    center = ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(
        ink,
        matrix,
        (width, height),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    # 旋转会把内容甩出画布；把内容质心平移回选区中心，尽量保留可见内容。
    region = _region(ink.shape, op.roi)
    ys, xs = np.where(rotated > 0)
    if len(xs) > 0:
        cx, cy = float(xs.mean()), float(ys.mean())
        tx = int(round((x0 + x1) / 2.0 - cx))
        ty = int(round((y0 + y1) / 2.0 - cy))
        if tx or ty:
            shifted = np.float32([[1, 0, tx], [0, 1, ty]])
            rotated = cv2.warpAffine(
                rotated,
                shifted,
                (width, height),
                flags=cv2.INTER_NEAREST,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=0,
            )
    return np.where(region > 0, rotated, ink)


def _apply_mirror(ink: np.ndarray, op: EditOp) -> np.ndarray:
    if op.roi is None:
        return cv2.flip(ink, 1)
    x0, y0, x1, y1 = _inside(op.roi, ink.shape[1], ink.shape[0])
    out = ink.copy()
    out[y0:y1, x0:x1] = cv2.flip(ink[y0:y1, x0:x1], 1)
    return out


def _apply_morph(ink: np.ndarray, op: EditOp, erode: bool) -> np.ndarray:
    radius = max(1, min(_LIMIT_VALUE, int(op.value or 1)))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (radius * 2 + 1, radius * 2 + 1))
    func = cv2.erode if erode else cv2.dilate
    done = func(ink, kernel)
    if op.roi is None:
        return done
    region = _region(ink.shape, op.roi)
    return np.where(region > 0, done, ink)


def apply_ops(ink: np.ndarray, ops: list[EditOp]) -> np.ndarray:
    for op in ops:
        if op.op == _OP_TRANSLATE:
            ink = _apply_translate(ink, op)
        elif op.op == _OP_SCALE:
            ink = _apply_scale(ink, op)
        elif op.op == _OP_ROTATE:
            ink = _apply_rotate(ink, op)
        elif op.op == _OP_MIRROR:
            ink = _apply_mirror(ink, op)
        elif op.op == _OP_THICKEN:
            ink = _apply_morph(ink, op, erode=False)
        elif op.op == _OP_THIN:
            ink = _apply_morph(ink, op, erode=True)
        elif op.op == _OP_CROP:
            region = _region(ink.shape, op.roi)
            ink = np.where(region > 0, ink, 255)
        elif op.op == _OP_ERASE:
            region = _region(ink.shape, op.roi)
            ink = np.where(region > 0, 255, ink)
        else:
            raise EditNotUnderstood(f"不支持的操作：{op.op}")
    return ink


def make_diff_image(base_path: Path, ink_new: np.ndarray, out_path: Path) -> None:
    """改动像素在灰底上标红，供前端草稿对照。"""
    base = np.asarray(Image.open(base_path).convert("L"), dtype=np.uint8)
    ink_old = (base < 128).astype(np.uint8)
    changed = (ink_old ^ (ink_new > 0).astype(np.uint8)) > 0
    rgb = np.stack([base, base, base], axis=-1)
    rgb[changed] = (214, 40, 40)
    Image.fromarray(rgb, mode="RGB").save(out_path)


class BuiltinSemanticProvider:
    id = "builtin-semantic"
    label = "本地语义编辑"
    supports = "semantic"

    def parse(self, instruction: str) -> list[EditOp]:
        return parse_instruction(instruction)

    def apply(self, image: Path, ops: list[EditOp], out_path: Path) -> Path:
        ink = apply_ops(load_ink(image), ops)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        save_ink(ink, out_path)
        return out_path

    def generate(
        self, image: Path, roi: dict | None, instruction: str, out_path: Path
    ) -> Path:
        raise NoGenerativeProvider("内置 provider 不含生成通道")


# ---------- 服务装配 ----------

@dataclass(frozen=True)
class DraftResult:
    channel: str
    provider_id: str
    parsed_ops: list[EditOp] | None
    draft_path: Path
    diff_path: Path | None
    ink_ratio: float


def discover_providers() -> list[EditProvider]:
    providers: list[EditProvider] = [BuiltinSemanticProvider()]
    try:
        entries = importlib.metadata.entry_points()
        group = (
            entries.select(group=ENTRY_POINT_GROUP)
            if hasattr(entries, "select")
            else entries.get(ENTRY_POINT_GROUP, [])
        )
        for entry in group:
            try:
                providers.append(entry.load()())
            except Exception:  # 第三方包损坏不能让应用起不来
                logger.exception("加载编辑 provider 失败：%s", entry.name)
    except Exception:
        logger.exception("枚举编辑 provider 失败")
    return providers


class EditService:
    def __init__(self, providers: list[EditProvider] | None = None) -> None:
        self._providers = list(providers) if providers is not None else discover_providers()

    def providers(self) -> list[EditProvider]:
        return list(self._providers)

    def get(self, provider_id: str) -> EditProvider:
        for provider in self._providers:
            if provider.id == provider_id:
                return provider
        raise UnknownProvider(f"未注册的编辑 provider：{provider_id}")

    def _resolve(self, provider_id: str | None) -> EditProvider:
        if provider_id:
            return self.get(provider_id)
        for provider in self._providers:
            if provider.supports in ("semantic", "both"):
                return provider
        raise EditNotUnderstood("没有可用的语义编辑 provider")

    def is_semantic(self, provider_id: str | None) -> bool:
        """路由用于决定基准图：语义通道以最新规划掩膜为基准（设计稿 2.1）。"""
        return self._resolve(provider_id).supports in ("semantic", "both")

    def create_draft(
        self,
        *,
        base_image: Path,
        instruction: str,
        roi: dict | None,
        provider_id: str | None,
        out_dir: Path,
    ) -> DraftResult:
        provider = self._resolve(provider_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        draft_path = out_dir / "draft.png"

        if provider.supports in ("semantic", "both"):
            ops = [op.with_roi(roi) for op in provider.parse(instruction)]
            provider.apply(base_image, ops, draft_path)
            ink = load_ink(draft_path)
            diff_path = out_dir / "diff.png"
            make_diff_image(base_image, ink, diff_path)
            return DraftResult(
                channel="semantic",
                provider_id=provider.id,
                parsed_ops=ops,
                draft_path=draft_path,
                diff_path=diff_path,
                ink_ratio=float((ink > 0).mean()),
            )

        if not any(p.supports in ("generative", "both") for p in self._providers):
            raise NoGenerativeProvider("生成通道尚未接入任何图像编辑 provider")
        generated = provider.generate(base_image, roi, instruction, draft_path)
        ink = load_ink(generated)
        return DraftResult(
            channel="generative",
            provider_id=provider.id,
            parsed_ops=None,
            draft_path=generated,
            diff_path=None,
            ink_ratio=float((ink > 0).mean()),
        )
