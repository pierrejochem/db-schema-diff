"""The GUI entry point, including its behaviour on a Python it cannot run on."""

from __future__ import annotations

import io
from contextlib import redirect_stderr
from unittest import mock

from cumo_schema_comparer.gui import MINIMUM_PYTHON
from cumo_schema_comparer.gui import __main__ as entry
from cumo_schema_comparer.gui.errors import GuiError


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
