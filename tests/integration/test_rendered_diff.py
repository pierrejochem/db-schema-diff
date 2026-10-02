"""A real multi-line definition has to render as a real diff.

Every unit test of the diff renderer builds its two sides by hand with ``\\n`` in them, and until
this file existed no test rendered a diff from a database at all. That combination hid the central
defect of the feature for twelve task reviews: a captured definition is canonicalised to its tokens
joined by single spaces, so on real data *every* diff was exactly one removed line and one added
line whatever the body's length, and context lines, the 60-line cap and the elision marker were all
unreachable.

So these assertions are not about the renderer, which has unit tests. They are about the whole path
from ``pg_get_viewdef`` to the HTML, which is the only place the defect was visible.
"""

from __future__ import annotations

import io
import re

import pytest

from cumo_schema_comparer.diff.model import ComparisonReport
from cumo_schema_comparer.report.html import HtmlReporter, _diff_for
from cumo_schema_comparer.report.textdiff import DiffKind, unified
from tests.integration.test_no_drift import compare

pytestmark = pytest.mark.integration

VIEW = "cumo-invoicing.invoice_summary"
ATTRIBUTE = "view.definition"
ROUTINE = "cumo-invoicing.invoice_gross"

#: Every rendered diff line in the HTML, as its CSS class suffix.
_RENDERED = re.compile(r'<span class="diff-([a-z]+)">')


def one_changed_line(databases, fixture: str):
    """One line of one multi-line definition changed in the target, nothing else touched."""
    databases.setup("base", drift=fixture)
    return compare(databases)


def delta_of(result, path_prefix: str, attribute: str):
    finding = next(f for f in result.findings if f.key.path.startswith(path_prefix))
    return next(d for d in finding.deltas if d.attribute == attribute)


def rendered_kinds(result) -> list[str]:
    report = ComparisonReport(name="invoicing", master_label="prod", targets=(result,))
    out = io.StringIO()
    HtmlReporter().render(report, out)
    return _RENDERED.findall(out.getvalue())


class TestTheComparedValueIsUnchanged:
    """The canonical value stays what equality is decided on; only the display changes."""

    def test_the_compared_values_are_still_the_canonical_one_liners(self, databases):
        delta = delta_of(one_changed_line(databases, "drift_view_body_one_line"), VIEW, ATTRIBUTE)
        assert "\n" not in delta.master_value
        assert "\n" not in delta.target_value
        assert delta.master_value != delta.target_value

    def test_one_changed_line_is_still_one_warning_and_nothing_else(self, databases):
        result = one_changed_line(databases, "drift_view_body_one_line")
        found = {(f.key.path, f.status.value, f.severity.label) for f in result.findings}
        assert found == {(VIEW, "differs", "warning")}


class TestTheRenderedDiffShowsTheChangedLine:
    def test_the_canonical_values_alone_could_only_ever_show_two_lines(self, databases):
        # The defect, pinned as the thing being fixed: diffing the compared values of a nine-line
        # view gives a hunk header, one removed line and one added line, and no context at all.
        delta = delta_of(one_changed_line(databases, "drift_view_body_one_line"), VIEW, ATTRIBUTE)
        kinds = [line.kind for line in unified(delta.master_value, delta.target_value)]
        assert kinds.count(DiffKind.REMOVED) == 1
        assert kinds.count(DiffKind.ADDED) == 1
        assert DiffKind.CONTEXT not in kinds

    def test_the_diff_has_more_than_two_lines_and_at_least_one_context_line(self, databases):
        delta = delta_of(one_changed_line(databases, "drift_view_body_one_line"), VIEW, ATTRIBUTE)
        lines = _diff_for(delta)
        kinds = [line.kind for line in lines]
        assert len(lines) > 2, lines
        assert DiffKind.CONTEXT in kinds, kinds
        # One line changed, so exactly one line is removed and one added; the rest is context.
        assert kinds.count(DiffKind.REMOVED) == 1
        assert kinds.count(DiffKind.ADDED) == 1
        assert any("gross_amount" in line.text for line in lines), lines

    def test_the_html_report_renders_more_than_two_diff_lines_with_context(self, databases):
        kinds = rendered_kinds(one_changed_line(databases, "drift_view_body_one_line"))
        assert len(kinds) > 2, kinds
        assert "context" in kinds
        assert kinds.count("removed") == 1
        assert kinds.count("added") == 1


class TestARoutineBodyToo:
    """A routine is compared by hash, so its text reaches the diff by a different route."""

    def test_a_changed_plpgsql_body_renders_every_changed_line_with_context(self, databases):
        # drift_function_body drops the function's whole IF block and changes the column it sums,
        # so a faithful diff has several removed lines. One removed line was the ceiling before.
        result = one_changed_line(databases, "drift_function_body")
        delta = delta_of(result, ROUTINE, "routine.body_hash")
        kinds = [line.kind for line in _diff_for(delta)]
        assert DiffKind.CONTEXT in kinds, kinds
        assert kinds.count(DiffKind.REMOVED) > 1, kinds
        assert kinds.count(DiffKind.ADDED) >= 1, kinds
