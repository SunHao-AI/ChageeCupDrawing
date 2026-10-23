"""规划服务：进程池执行、超时保护、预览资产与 paths.json 落盘。

``core`` 只算不写，落盘与超时控制都在这里完成，因此 API 层只需要一次同步调用。
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import numpy as np

from app.config import PLAN_TIMEOUT_S
from app.core.imaging.binarize import IMAGE_MODE_ADAPTIVE, normalize_image_mode
from app.core.imaging.preprocess import load_grayscale
from app.core.planning.planner import build_drawing_plan
from app.core.planning.preview import make_composite, make_difference_image


class PlanTimeout(RuntimeError):
    """规划超过时限。"""


class PlanFailed(RuntimeError):
    """掩膜为空、路径数为零或子进程异常。"""


def _inner_size(canvas_w: int, canvas_h: int, margin: int) -> tuple[int, int]:
    # 与 build_drawing_plan 内部的取法保持一致，合成图才能对齐掩膜。
    return max(24, canvas_w - margin * 2), max(36, canvas_h - margin * 2)


# 自适应模式要对照片掩膜做骨架细化与回环救援，耗时随工作面积、路径复杂度
# 暴涨，而质量在更大倍率下并不继续提升（实测 529×796 画布 + 604×755 照片：
# ss=3 需 53.6s / IoU 0.767，ss=2 需 19.4s / IoU 0.792）。超过约 2MP 的
# 工作面积继续加大倍率只剩成本没有收益。
_ADAPTIVE_MAX_WORK_AREA = 2_000_000
_ADAPTIVE_MAX_SS = 2


def _effective_supersample(
    supersample: int, target_w: int, target_h: int, image_mode: str
) -> int:
    """自适应模式的超采样上限保护（只降不升，其余模式尊重用户取值）。"""
    ss = max(1, min(4, int(supersample)))
    if ss <= _ADAPTIVE_MAX_SS:
        return ss
    if normalize_image_mode(image_mode) != IMAGE_MODE_ADAPTIVE:
        return ss
    if target_w * target_h * ss * ss <= _ADAPTIVE_MAX_WORK_AREA:
        return ss
    return _ADAPTIVE_MAX_SS


def _plan_worker(job: dict) -> dict:
    """子进程入口：只接收可 pickle 的原生结构，结果也只回传原生结构。"""
    params = job["params"]
    image = load_grayscale(job["image_path"])
    target_w = int(job["target_w"])
    target_h = int(job["target_h"])
    work_scale = _effective_supersample(
        params["supersample"], target_w, target_h, params["imageMode"]
    )

    plan = build_drawing_plan(
        image,
        target_w,
        target_h,
        image_mode=params["imageMode"],
        threshold=params["threshold"],
        contrast=params["contrast"],
        fit_mode=params["fitMode"],
        line_expand=params["lineExpand"],
        strategy=params["strategy"],
        brush_width=params["brushWidth"],
        simplify_px=params["simplifyPx"],
        safe_margin=params["safeMargin"],
        fill_density=params["fillDensity"],
        legacy_row_step=params["legacyRowStep"],
        supersample=work_scale,
        detail_keep=params["detailKeep"],
        thin_finish=True,
        auto_trim=params["autoTrim"],
        topology_rescue=params["topologyRescue"],
        target_recall=params["targetRecall"],
        threshold_mode=params["thresholdMode"],
        hatch_levels=params.get("hatchLevels", 10),
    )
    if not plan.paths:
        return {"ok": False, "error": "plan_empty"}

    out_dir = Path(job["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    inner_w, inner_h = _inner_size(target_w, target_h, plan.margin)
    paths = [[[int(round(x)), int(round(y))] for x, y in path] for path in plan.paths]

    plan.binary.save(out_dir / "mask.png")
    plan.simulated.save(out_dir / "simulated.png")
    make_difference_image(plan.binary, plan.simulated).save(out_dir / "difference.png")
    make_composite(
        image,
        plan.simulated,
        inner_w=inner_w,
        inner_h=inner_h,
        fit_mode=params["fitMode"],
        contrast=params["contrast"],
        margin=plan.margin,
    ).save(out_dir / "composite.png")
    (out_dir / "paths.json").write_text(
        json.dumps(paths, ensure_ascii=False), encoding="utf-8"
    )

    ink = float((np.asarray(plan.binary, dtype=np.uint8) < 128).mean())
    return {
        "ok": True,
        "strategy": plan.strategy,
        "metrics": {
            "precision": round(float(plan.precision), 6),
            "recall": round(float(plan.recall), 6),
            "iou": round(float(plan.iou), 6),
            "pathCount": len(paths),
            "totalLength": round(float(plan.total_length), 2),
            "correctionPaths": int(plan.correction_paths),
            "workScale": int(plan.work_scale),
            "inkRatio": round(ink, 6),
        },
    }


class PlanningService:
    """惰性创建的单 worker 进程池；超时后丢弃池，避免后续请求继续排队。"""

    def __init__(self, timeout_s: float = PLAN_TIMEOUT_S) -> None:
        self._timeout_s = float(timeout_s)
        self._pool: ProcessPoolExecutor | None = None
        self._lock = threading.Lock()

    def _ensure_pool(self) -> ProcessPoolExecutor:
        with self._lock:
            if self._pool is None:
                self._pool = ProcessPoolExecutor(max_workers=1)
            return self._pool

    def _discard_pool(self) -> None:
        with self._lock:
            pool = self._pool
            self._pool = None
        if pool is None:
            return
        pool.shutdown(wait=False, cancel_futures=True)

    def run(
        self,
        *,
        image_path: Path,
        target_w: int,
        target_h: int,
        params: dict,
        out_dir: Path,
    ) -> dict:
        job = {
            "image_path": str(image_path),
            "target_w": int(target_w),
            "target_h": int(target_h),
            "params": params,
            "out_dir": str(out_dir),
        }
        future = self._ensure_pool().submit(_plan_worker, job)
        try:
            result = future.result(timeout=self._timeout_s)
        except FuturesTimeout as exc:
            self._discard_pool()
            raise PlanTimeout(f"规划超过 {self._timeout_s:.0f} s") from exc
        except BrokenProcessPool as exc:
            self._discard_pool()
            raise PlanFailed(f"规划进程异常退出：{exc}") from exc
        except Exception as exc:
            raise PlanFailed(f"规划失败：{exc}") from exc

        if not result.get("ok"):
            raise PlanFailed("掩膜为空或未生成任何路径")
        return result

    def close(self) -> None:
        with self._lock:
            pool = self._pool
            self._pool = None
        if pool is not None:
            pool.shutdown(wait=True)
