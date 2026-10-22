"""I13 预设读写。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.errors import ApiError
from app.api.schemas import PresetCreate
from app.store.models import Preset

router = APIRouter()


def _preset(preset: Preset) -> dict[str, object]:
    return {
        "presetId": preset.id,
        "name": preset.name,
        "params": preset.params,
        "builtin": preset.builtin,
    }


@router.get("/presets")
def list_presets(request: Request) -> list[dict[str, object]]:
    return [_preset(preset) for preset in request.app.state.store.list_presets()]


@router.post("/presets", status_code=201)
def create_preset(payload: PresetCreate, request: Request) -> dict[str, object]:
    name = payload.name.strip()
    if not name:
        raise ApiError(400, "E_INVALID_ARGUMENT", "预设名不能为空")
    preset = request.app.state.store.create_preset(
        name=name, params=payload.params.model_dump(by_alias=True)
    )
    return _preset(preset)
