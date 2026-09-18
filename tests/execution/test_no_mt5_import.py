"""Hygiene: execution package must not import MetaTrader5."""

from __future__ import annotations

import ast
from pathlib import Path


def _imports_mt5(source: str) -> bool:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "MetaTrader5":
                    return True
        elif isinstance(node, ast.ImportFrom):
            if node.module == "MetaTrader5":
                return True
    return False


def test_execution_package_has_no_metatrader5_imports():
    root = Path(__file__).resolve().parents[2] / "src" / "q_backend" / "execution"
    offenders = []
    for path in root.rglob("*.py"):
        if _imports_mt5(path.read_text()):
            offenders.append(str(path.relative_to(root.parent.parent)))
    assert offenders == []
