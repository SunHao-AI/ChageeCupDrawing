"""金标准回归：core 与 V6 单文件实现在同一输入上必须逐位一致。

P0 退出条件（design.md 8.3）：200 张样例的掩膜、路径数、precision/recall/IoU
与 V6 完全一致。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from conftest import (
    EXAMPLES_DIR,
    FIT_MODES_V6,
    IMAGE_MODES,
    IMAGE_MODES_V6,
    STRATEGIES_V6,
    VARIANTS_PER_IMAGE,
    derive_variant,
    load_grayscale,
)

from app.core.imaging.binarize import process_image_hq as core_process_image_hq
from app.core.planning.planner import build_drawing_plan as core_build_drawing_plan
from app.core.vision.canvas_detect import detect_canvas_rect as core_detect_canvas_rect


def _cases() -> list[tuple[str, Path, int, int]]:
    cases: list[tuple[str, Path, int, int]] = []
    index = 0
    for path in sorted(EXAMPLES_DIR.glob("*.png")):
        for variant in range(VARIANTS_PER_IMAGE):
            cases.append((f"{path.stem}-v{variant:02d}", path, variant, index))
            index += 1
    return cases


def _plan_params(index: int) -> dict:
    from conftest import plan_params

    return plan_params(index)


CASES = _cases()


@pytest.mark.parametrize(
    "sample_id,image_path,variant,index",
    CASES,
    ids=[case[0] for case in CASES],
)
def test_plan_matches_v6(v6, sample_id, image_path, variant, index):
    params = _plan_params(index)
    image = derive_variant(load_grayscale(image_path), variant)
    canvas_w, canvas_h = params["canvas"]

    common = {
        "threshold": params["threshold"],
        "contrast": params["contrast"],
        "line_expand": params["line_expand"],
        "brush_width": params["brush_width"],
        "simplify_px": params["simplify_px"],
        "safe_margin": params["safe_margin"],
        "fill_density": params["fill_density"],
        "legacy_row_step": params["legacy_row_step"],
        "supersample": params["supersample"],
        "detail_keep": params["detail_keep"],
        "thin_finish": params["thin_finish"],
        "auto_trim": params["auto_trim"],
        "topology_rescue": params["topology_rescue"],
        "target_recall": params["target_recall"],
    }

    expected = v6.build_drawing_plan(
        image,
        canvas_w,
        canvas_h,
        image_mode=IMAGE_MODES_V6[params["image_mode"]],
        fit_mode=FIT_MODES_V6[params["fit_mode"]],
        strategy=STRATEGIES_V6[params["strategy"]],
        **common,
    )
    actual = core_build_drawing_plan(
        image,
        canvas_w,
        canvas_h,
        image_mode=params["image_mode"],
        fit_mode=params["fit_mode"],
        strategy=params["strategy"],
        **common,
    )

    context = (
        f"{sample_id} strategy={params['strategy']} mode={params['image_mode']} "
        f"canvas={canvas_w}x{canvas_h} ss={params['supersample']}"
    )
    assert actual.margin == expected.margin, context
    assert actual.work_scale == expected.work_scale, context
    assert actual.correction_paths == expected.correction_paths, context
    assert actual.binary.tobytes() == expected.binary.tobytes(), context
    assert actual.simulated.tobytes() == expected.simulated.tobytes(), context
    assert len(actual.paths) == len(expected.paths), context
    for i, (got, want) in enumerate(zip(actual.paths, expected.paths)):
        assert len(got) == len(want), f"{context} path#{i}"
        np.testing.assert_allclose(np.asarray(got), np.asarray(want), rtol=0, atol=0, err_msg=f"{context} path#{i}")
    assert actual.total_length == expected.total_length, context
    assert (actual.precision, actual.recall, actual.iou) == (
        expected.precision,
        expected.recall,
        expected.iou,
    ), context


def _mode_matrix_cases():
    paths = sorted(EXAMPLES_DIR.glob("*.png"))
    return [(p, m) for p in paths for m in IMAGE_MODES]


@pytest.mark.parametrize("supersample", [1, 3])
@pytest.mark.parametrize(
    "image_path,image_mode",
    _mode_matrix_cases(),
    ids=[f"{p.stem}-{m}" for p, m in _mode_matrix_cases()],
)
def test_work_mask_matches_v6(v6, image_path, image_mode, supersample):
    image = load_grayscale(image_path)
    target_w, target_h = 220, 300

    expected_binary, expected_mask, expected_ss = v6.process_image_hq(
        image,
        target_w,
        target_h,
        IMAGE_MODES_V6[image_mode],
        205,
        1.0,
        "完整显示（留白）",
        0,
        supersample,
        0.14,
        True,
    )
    actual_binary, actual_mask, actual_ss = core_process_image_hq(
        image,
        target_w,
        target_h,
        image_mode,
        205,
        1.0,
        "contain",
        0,
        supersample,
        0.14,
        True,
    )

    context = f"{image_path.stem} mode={image_mode} ss={supersample}"
    assert actual_ss == expected_ss, context
    assert actual_binary.tobytes() == expected_binary.tobytes(), context
    assert np.array_equal(actual_mask, expected_mask), context


@pytest.mark.parametrize(
    "image_path",
    sorted(EXAMPLES_DIR.glob("*.png")),
    ids=[p.stem for p in sorted(EXAMPLES_DIR.glob("*.png"))],
)
def test_detect_canvas_rect_matches_v6(v6, image_path):
    image = load_grayscale(image_path)
    for scale in (0.5, 1.0, 1.5):
        if scale == 1.0:
            sample = image
        else:
            size = (max(8, round(image.width * scale)), max(8, round(image.height * scale)))
            sample = image.resize(size, Image.Resampling.BICUBIC)
        assert core_detect_canvas_rect(sample) == v6.detect_canvas_rect(sample), (
            f"{image_path.stem}@{scale}"
        )
