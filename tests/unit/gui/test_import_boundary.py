"""The GUI is a presentation layer, and that is checkable rather than merely asserted.

A GUI that reached into `db` or `normalize` would start reimplementing the comparison. Walking the
imports is how that stays true as the package grows.
"""

from __future__ import annotations

import ast
import pathlib
import tempfile

import pytest

GUI = pathlib.Path(__file__).resolve().parents[3] / "src" / "cumo_schema_comparer" / "gui"

FORBIDDEN = (
    "cumo_schema_comparer.db",
    "cumo_schema_comparer.normalize",
    "cumo_schema_comparer.build",
    "cumo_schema_comparer.model.objects",
    "cumo_schema_comparer.model.inventory",
    "cumo_schema_comparer.diff.engine",
)


def gui_modules():
    return sorted(GUI.rglob("*.py"))


def imported_names(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                # Absolute import: record the module and each imported name.
                names.add(node.module)
                for alias in node.names:
                    names.add(f"{node.module}.{alias.name}")
            elif node.level > 0:
                # Relative import: resolve against cumo_schema_comparer.gui.
                base_parts = ["cumo_schema_comparer", "gui"]
                # Walk up by node.level - 1 (level 1 is same package, level 2 is parent, etc.)
                base_parts = base_parts[: -(node.level - 1)] if node.level > 1 else base_parts
                if node.module:
                    resolved = ".".join(base_parts) + "." + node.module
                else:
                    resolved = ".".join(base_parts)
                names.add(resolved)
                # Also record each imported name from the resolved module.
                for alias in node.names:
                    names.add(f"{resolved}.{alias.name}")
    return names


def test_there_are_gui_modules_to_check():
    assert gui_modules(), "the boundary test would pass vacuously with no modules"


def test_the_boundary_checker_catches_forbidden_imports():
    """Verify the checker can detect forbidden imports, proving it is not broken."""
    source_code = """
from .. import db
from ..db import connect
from cumo_schema_comparer import normalize
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(source_code)
        temp_path = pathlib.Path(f.name)
    try:
        names = imported_names(temp_path)
        # The checker should have found these forbidden imports.
        assert "cumo_schema_comparer.db" in names
        assert "cumo_schema_comparer.db.connect" in names
        assert "cumo_schema_comparer.normalize" in names
    finally:
        temp_path.unlink()


@pytest.mark.parametrize("path", gui_modules(), ids=lambda p: p.name)
def test_no_gui_module_imports_the_comparison_internals(path):
    offending = [
        name
        for name in imported_names(path)
        for forbidden in FORBIDDEN
        if name == forbidden or name.startswith(forbidden + ".")
    ]
    assert offending == [], f"{path} imports {offending}"
