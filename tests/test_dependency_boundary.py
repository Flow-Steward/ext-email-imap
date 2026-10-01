from __future__ import annotations

import ast
from pathlib import Path

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_FILES = tuple(
    path for path in sorted(BUNDLE_ROOT.glob("*.py")) if path.name != "__init__.py"
)


def test_runtime_boundary_has_no_private_imports_or_sys_path_changes() -> None:
    assert RUNTIME_FILES
    for path in RUNTIME_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(
                    alias.name != "core" and not alias.name.startswith("core.")
                    for alias in node.names
                ), f"{path.name} imports private core code"
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert module != "core", f"{path.name} imports private core code"
                assert not module.startswith("core."), f"{path.name} imports private core code"
            elif isinstance(node, ast.Attribute):
                assert not (
                    isinstance(node.value, ast.Name)
                    and node.value.id == "sys"
                    and node.attr == "path"
                ), f"{path.name} mutates or reads sys.path"
