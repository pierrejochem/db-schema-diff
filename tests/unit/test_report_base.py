"""The reporter contract."""

from __future__ import annotations

import io

from db_schema_comparer.diff.model import ComparisonReport
from db_schema_comparer.report.base import Reporter, render_to_path
from db_schema_comparer.report.console import ConsoleReporter


class Fake:
    name = "fake"

    def render(self, report: ComparisonReport, out) -> None:
        out.write(f"rendered {report.name}\n")


def test_the_console_reporter_satisfies_the_protocol():
    # runtime_checkable, so this is a real structural check and not a comment.
    assert isinstance(ConsoleReporter(), Reporter)


def test_render_to_path_writes_the_file_and_creates_directories(tmp_path):
    destination = tmp_path / "nested" / "deeper" / "out.txt"
    render_to_path(Fake(), ComparisonReport(name="invoicing", master_label="prod"), destination)
    assert destination.read_text() == "rendered invoicing\n"


def test_a_reporter_only_needs_the_report():
    # No connection, no config, no clock: that restriction is what makes `render --from` possible.
    out = io.StringIO()
    ConsoleReporter().render(ComparisonReport(name="invoicing", master_label="prod"), out)
    text = out.getvalue()
    assert "invoicing" in text
    assert "prod" in text
