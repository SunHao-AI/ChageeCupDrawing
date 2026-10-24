"""应用级常量与运行配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

APP_VERSION = "0.1.0"

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_IMAGE_LONG_SIDE = 6000
MAX_PROJECT_ASSET_BYTES = 200 * 1024 * 1024

SESSION_COOKIE = "cupdraw_session"
SESSION_HEADER = "X-Requested-With"
SESSION_HEADER_VALUE = "cupdraw"

PLAN_TIMEOUT_S = 120.0


def default_data_dir() -> Path:
    override = os.environ.get("CUPDRAW_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "data"


@dataclass
class Settings:
    data_dir: Path = field(default_factory=default_data_dir)
    plan_timeout_s: float = PLAN_TIMEOUT_S
    static_dir: Path | None = None
    # 为空时启动生成；测试可注入固定值以断言鉴权行为。
    session_token: str | None = None
    # 默认只监听回环。CUPDRAW_BIND=0.0.0.0 开启局域网模式，供手机/平板控制。
    bind_host: str = field(default_factory=lambda: os.environ.get("CUPDRAW_BIND", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.environ.get("CUPDRAW_PORT", "8765")))
