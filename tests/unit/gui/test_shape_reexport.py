"""gui/shape.py keeps its name so GUI callers are untouched, but owns no logic any more."""

import pytest

pytest.importorskip("slint", reason="the GUI extra is not installed")


def test_the_gui_detector_is_the_library_one():
    from db_schema_diff import literals
    from db_schema_diff.gui import shape

    assert shape.looks_like_connection_string is literals.looks_like_connection_string
