"""The GUI is a presentation layer, and that is checkable rather than merely asserted.

A GUI that reached into `db` or `normalize` would start reimplementing the comparison. Walking the
imports is how that stays true as the package grows.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

GUI = pathlib.Path("src/cumo_schema_comparer/gui")

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
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
        elif isinstance(node, ast.ImportFrom) and node.level:
            # A relative import inside gui/ resolves within the package, never outside it.
            names.add("cumo_schema_comparer.gui")
    return names


def test_there_are_gui_modules_to_check():
    assert gui_modules(), "the boundary test would pass vacuously with no modules"


@pytest.mark.parametrize("path", gui_modules(), ids=lambda p: p.name)
def test_no_gui_module_imports_the_comparison_internals(path):
    offending = [
        name
        for name in imported_names(path)
        for forbidden in FORBIDDEN
        if name == forbidden or name.startswith(forbidden + ".")
    ]
    assert offending == [], f"{path} imports {offending}"
