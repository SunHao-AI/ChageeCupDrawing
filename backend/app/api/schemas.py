"""请求/响应数据结构。字段名对外统一为 camelCase。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class PlanParams(CamelModel):
    """决定「画什么」的参数；默认值与 V6 保持一致。"""

    image_mode: Literal["fidelity", "adaptive", "edge", "dither"] = "fidelity"
    threshold: int = Field(default=205, ge=90, le=230)
    contrast: float = Field(default=1.0, ge=0.6, le=2.2)
    fit_mode: Literal["contain", "cover"] = "contain"
    line_expand: int = Field(default=0, ge=-2, le=3)
    threshold_mode: bool = False
    auto_trim: bool = True
    strategy: Literal["topology", "centerline", "contour", "hscan", "tone"] = "topology"
    brush_width: int = Field(default=3, ge=1, le=12)
    simplify_px: float = Field(default=0.16, ge=0.1, le=2.0)
    safe_margin: int = Field(default=4, ge=2, le=25)
    fill_density: float = Field(default=0.82, ge=0.62, le=1.10)
    legacy_row_step: int = Field(default=3, ge=1, le=20)
    supersample: int = Field(default=3, ge=1, le=4)
    detail_keep: float = Field(default=0.14, ge=0.05, le=0.60)
    topology_rescue: bool = True
    target_recall: float = Field(default=0.985, ge=0.95, le=0.998)
    # 照片素描策略的色调分层数：越大暗部层次越细、笔迹越多。
    hatch_levels: int = Field(default=10, ge=6, le=24)
    # 照片素描碎线过滤：短于此长度（画布 px）的等值线视为「梯子横档」噪声丢弃。
    # 0=不过滤（保留最密线条），越大画面越干净但细节越少。
    min_stroke_px: int = Field(default=25, ge=0, le=120)


class RectModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: int = Field(ge=0)
    y: int = Field(ge=0)
    w: int = Field(gt=0)
    h: int = Field(gt=0)


class PointModel(CamelModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)


class CalibrationRequest(CamelModel):
    hwnd: int = Field(gt=0)
    mode: Literal["auto", "manual"] = "auto"
    rect: RectModel | None = None
    capture_method: Literal["maa", "win32"] | None = None
    # 绘制目标类型：pc=本机窗口，mirror=投屏窗口（adb 直连为后续预留）。
    device_kind: Literal["pc", "mirror"] = "pc"
    device_serial: str | None = Field(default=None, max_length=64)
    # 自动变粗选填项（截图坐标系）：笔粗细滑杆轨道区域与撤销按钮中心点。
    slider: RectModel | None = None
    undo: PointModel | None = None
    # 画板下方工具面板折叠按钮中心点：换笔间隙自动收起面板，让画布完整可见。
    fold: PointModel | None = None
    # 面板展开/折叠两态工具位图（ToolbarProber 探测产物）：结构由 executor 保证，
    # API 层不强校验、原样透传入库。
    tools_open: dict | None = None
    tools_collapsed: dict | None = None


class PlanRequest(CamelModel):
    params: PlanParams = Field(default_factory=PlanParams)


class MirrorStartRequest(CamelModel):
    serial: str = Field(min_length=1, max_length=64)


class MirrorRegisterRequest(CamelModel):
    hwnd: int = Field(gt=0)
    serial: str = Field(min_length=1, max_length=64)
    label: str = Field(default="", max_length=64)


class PresetCreate(CamelModel):
    name: str = Field(min_length=1, max_length=64)
    params: PlanParams = Field(default_factory=PlanParams)


class ExecParamsModel(CamelModel):
    """决定「怎么画」的参数；范围继承 V6 滑块语义。"""

    event_step_px: float = Field(default=1.0, ge=0.8, le=5.0)
    point_delay_ms: float = Field(default=1.5, ge=0.5, le=10.0)
    stroke_delay_ms: float = Field(default=6.0, ge=0.0, le=50.0)
    click_hold_ms: float = Field(default=20.0, ge=10.0, le=200.0)
    no_coalesce: bool = True
    short_stroke_protect: bool = True
    short_stroke_threshold_px: float = Field(default=1.2, ge=0.0, le=20.0)
    focus_guard: bool = True
    cursor_takeover_px: float = Field(default=6.0, ge=2.0, le=100.0)
    guard_poll_ms: float = Field(default=50.0, ge=10.0, le=500.0)
    # 自动变粗：绘制前自动标定小程序粗细滑杆并分档换笔（需校准页框选滑杆）。
    auto_width: bool = False
    width_tiers: int = Field(default=5, ge=2, le=10)


class TaskCreate(CamelModel):
    exec: ExecParamsModel = Field(default_factory=ExecParamsModel)
    countdown_ms: int = Field(default=3000, ge=0, le=30000)
    allow_drift: float = Field(default=0.02, ge=0.0, le=0.5)


class TaskResume(CamelModel):
    countdown_ms: int = Field(default=3000, ge=0, le=30000)
    allow_drift: float = Field(default=0.02, ge=0.0, le=0.5)


class EditRequest(CamelModel):
    """I17 发起编辑。roi 为源图像素坐标；baseVersion 防止基于过期版本提交。"""

    instruction: str = Field(min_length=1, max_length=500)
    roi: RectModel | None = None
    provider: str | None = None
    base_version: int = Field(ge=1)
    # auto：规则优先、生成兜底（设计文档 ai-edit-provider §2）。
    channel: Literal["auto", "semantic", "generative"] = "auto"


class RevertRequest(CamelModel):
    """I22 复原到历史源图版本。"""

    target_version: int = Field(ge=1)


class AiSettingsUpdate(CamelModel):
    """I20 写入 AI 配置（不含 Key）。

    api_key 仅为兼容旧前端保留，服务端忽略且绝不落盘；Key 走请求头 X-AI-Key。
    """

    provider: str | None = None
    model: str | None = None
    api_key: str | None = None
    # OpenAI 兼容通道的服务地址；留空表示不启用该通道。
    base_url: str | None = None
    allow_cloud_edit: bool = False


class AiProbeRequest(CamelModel):
    """I21 预检：用表单当前值验证，无需先保存；缺省字段沿用已保存配置。

    Key 从请求头 X-AI-Key 读取，请求体不再接收明文 Key。
    """

    provider: str | None = None
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    allow_cloud_edit: bool | None = None
