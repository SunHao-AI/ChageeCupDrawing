"""模块边界：core 只依赖算法库，不得反向依赖设备、执行器或 API 层。"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

CORE_DIR = Path(__file__).resolve().parents[1] / "app" / "core"
FORBIDDEN_PREFIXES = ("app.device", "app.executor", "app.api", "app.store", "maa")


def _core_modules() -> list[Path]:
    return sorted(CORE_DIR.rglob("*.py"))


def _imported_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
    return names


@pytest.mark.parametrize(
    "module_path",
    _core_modules(),
    ids=[str(p.relative_to(CORE_DIR.parent.parent)) for p in _core_modules()],
)
def test_core_does_not_import_outer_layers(module_path):
    offenders = [
        name
        for name in _imported_names(module_path)
        if any(name == prefix or name.startswith(prefix + ".") for prefix in FORBIDDEN_PREFIXES)
    ]
    assert not offenders, f"{module_path.name} 反向依赖了 {offenders}"
