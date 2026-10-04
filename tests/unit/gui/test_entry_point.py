"""The GUI start-up, including its behaviour on a Python it cannot run on.

The start-up lives in `launcher`, not `__main__`: a compiled build cannot import a module named
`__main__` without colliding with the program's own. `__main__` is a shim over it.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr
from unittest import mock

from db_schema_diff.gui import MINIMUM_PYTHON
from db_schema_diff.gui import launcher as entry
from db_schema_diff.gui.errors import GuiError


def test_the_floor_is_python_312():
    # Slint's Python binding requires 3.12; the CLI still supports 3.11.
    assert MINIMUM_PYTHON == (3, 12)


def test_an_unsupported_python_exits_with_a_message_not_an_import_error():
    stderr = io.StringIO()
    with mock.patch.object(entry.sys, "version_info", (3, 11, 9)), redirect_stderr(stderr):
        code = entry.main()
    assert code == 2
    message = stderr.getvalue()
    assert "3.12" in message
    assert "Traceback" not in message


def test_a_gui_error_carries_an_optional_field_path():
    error = GuiError("dsn_env is required", field="targets.1.dsn_env")
    assert error.field == "targets.1.dsn_env"
    assert str(error) == "dsn_env is required"
    assert GuiError("boom").field is None


def test_the_dunder_main_shim_only_forwards():
    """`python -m db_schema_diff.gui` must keep working, and must hold no logic of its own.

    Logic in both places would drift, and the compiled binary only ever runs the launcher.
    """
    import ast
    from pathlib import Path

    from db_schema_diff.gui import launcher

    source = Path(launcher.__file__).with_name("__main__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert "from .launcher import main" in source
    # A docstring, one import, and the `if __name__` guard. Nothing else belongs here.
    assert [type(node).__name__ for node in tree.body] == [
        "Expr",
        "ImportFrom",
        "ImportFrom",
        "If",
    ]
