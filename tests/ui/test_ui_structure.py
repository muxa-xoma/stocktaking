"""Structural regression tests for UI page classes.

Verifies that no nested function definitions (`FunctionDef` / `AsyncFunctionDef`)
remain inside any UI page class method. This guards against future
re-introduction of nested functions in refactored UI files.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

UI_FILES = [
    "pages_bonds.py",
    "pages_accounts.py",
    "pages_brokers.py",
    "pages_home.py",
    "pages_transactions.py",
    "pages_analytics.py",
    "pages_auth.py",
    "base_page.py",
    "crud_mixin.py",
]

UI_DIR = Path(__file__).resolve().parents[2] / "src" / "bond_accounting" / "ui"


def _iter_defs(node: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return direct child FunctionDef/AsyncFunctionDef nodes of ``node`` body."""
    defs: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.append(child)
    return defs


def assert_no_nested_functions(filepath: Path) -> None:
    """Assert no function definitions are nested inside any class method body."""
    source = filepath.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(filepath))

    nested: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for method in _iter_defs(node):
            for nested_def in _iter_defs(method):
                nested.append((nested_def.name, nested_def.lineno))

    assert not nested, (
        f"Found {len(nested)} nested function definition(s) in {filepath}:\n"
        + "\n".join(f"  - {name} (line {lineno})" for name, lineno in nested)
    )


@pytest.mark.parametrize("filename", UI_FILES)
def test_no_nested_functions(filename: str) -> None:
    """Detect any nested function definitions inside UI page class methods."""
    filepath = UI_DIR / filename
    assert_no_nested_functions(filepath)
