"""Reports whose findings carry body deltas, for the HTML diff rendering."""

from __future__ import annotations

import pytest

from cumo_schema_comparer.diff.model import (
    AttributeDelta,
    ComparisonReport,
    ObjectFinding,
    ObjectStatus,
    TargetDiff,
)
from cumo_schema_comparer.diff.severity import Severity
from cumo_schema_comparer.model.keys import table_key
from tests.support.builders import source


def _report(*deltas: AttributeDelta) -> ComparisonReport:
    target = TargetDiff(
        master_label="prod",
        target_label="qa",
        master_source=source("prod"),
        target_source=source("qa"),
        findings=(
            ObjectFinding(
                key=table_key("public", "thing"),
                status=ObjectStatus.DIFFERS,
                severity=Severity.ERROR,
                deltas=deltas,
            ),
        ),
    )
    return ComparisonReport(
        name="diffs",
        master_label="prod",
        targets=(target,),
        generated_at="2026-01-15T09:30:00+00:00",
        tool_version="0.1.0",
    )


def _body(master: str | None, target: str | None, **extra: object) -> AttributeDelta:
    return AttributeDelta(
        attribute="view.definition",
        master_value=master,
        target_value=target,
        severity=Severity.ERROR,
        body=True,
        **extra,  # type: ignore[arg-type]
    )


@pytest.fixture
def a_report_with_changed_view() -> ComparisonReport:
    return _report(_body("SELECT a,\n       b\n  FROM t", "SELECT a,\n       c\n  FROM t"))


@pytest.fixture
def a_report_with_changed_column_type() -> ComparisonReport:
    return _report(
        AttributeDelta(
            attribute="column.data_type",
            master_value="varchar(40)",
            target_value="text",
            severity=Severity.ERROR,
        )
    )


@pytest.fixture
def a_report_with_a_huge_body_change() -> ComparisonReport:
    master = "\n".join(f"line {i}" for i in range(200))
    target = "\n".join(f"line {i} changed" for i in range(200))
    return _report(_body(master, target))


@pytest.fixture
def a_report_with_angle_brackets_in_a_body() -> ComparisonReport:
    return _report(
        _body("SELECT 1\n-- <script>alert(1)</script>", "SELECT 2\n-- <script>x</script>")
    )


@pytest.fixture
def a_report_with_a_hashed_routine() -> ComparisonReport:
    """A version-1 inventory: the body was not captured, so only the hashes exist."""
    return _report(
        AttributeDelta(
            attribute="routine.body",
            master_value="a" * 64,
            target_value="b" * 64,
            severity=Severity.ERROR,
            body=True,
        )
    )


@pytest.fixture
def a_report_with_a_captured_routine() -> ComparisonReport:
    return _report(
        AttributeDelta(
            attribute="routine.body",
            master_value="a" * 64,
            target_value="b" * 64,
            severity=Severity.ERROR,
            body=True,
            master_display="BEGIN RETURN 1; END",
            target_display="BEGIN RETURN 2; END",
        )
    )


@pytest.fixture
def a_report_with_masked_values() -> ComparisonReport:
    return _report(
        _body(
            "CREATE SERVER s\n  OPTIONS (password '***:a3f1c9')",
            "CREATE SERVER s\n  OPTIONS (password '***:b7c2d0')",
        )
    )


@pytest.fixture
def a_report_with_a_50kb_one_line_body() -> ComparisonReport:
    return _report(
        AttributeDelta(
            attribute="routine.body",
            master_value="a" * 64,
            target_value="b" * 64,
            severity=Severity.ERROR,
            body=True,
            master_display="SELECT " + "x, " * 20000,
            target_display="SELECT " + "y, " * 20000,
        )
    )


@pytest.fixture
def a_report_with_a_50kb_multiline_body() -> ComparisonReport:
    master = "\n".join(f"statement number {i} -- {'p' * 40}" for i in range(1000))
    target = "\n".join(f"statement number {i} -- {'q' * 40}" for i in range(1000))
    return _report(_body(master, target))


@pytest.fixture
def a_report_with_a_long_multiline_scalar() -> ComparisonReport:
    """A scalar (non-body) delta that is long and multi-line, e.g. a column default."""
    return _report(
        AttributeDelta(
            attribute="column.default",
            master_value="CASE\n  WHEN a THEN 1\n  ELSE 2 END",
            target_value="CASE\n  WHEN a THEN 1\n  ELSE 3 END " + "x" * 200,
            severity=Severity.WARNING,
        )
    )


@pytest.fixture
def a_report_with_a_directional_change() -> ComparisonReport:
    return _report(_body("keep\nonly_in_master\nkeep2", "keep\nonly_in_target\nkeep2"))
