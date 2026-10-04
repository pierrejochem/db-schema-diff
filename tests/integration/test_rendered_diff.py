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

from db_schema_comparer.diff.model import ComparisonReport
from db_schema_comparer.report.html import HtmlReporter, _diff_for
from db_schema_comparer.report.textdiff import DiffKind, unified
from tests.integration.conftest import apply_sql
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


COMPUTED = "cumo-invoicing.column_flavours.computed"
SERIAL = "cumo-invoicing.column_flavours.small_serial"


class TestAGeneratedColumnIsNotADefault:
    """A stored generated column's expression lives in the same catalog slot as a default.

    Which made the raw text land under ``default`` while the value compared as ``default`` was
    ``None`` — so the pane printed a generation expression beside an empty compared value. Now that
    the display reads the raw text, the key the text is filed under is what a reader sees.
    """

    @staticmethod
    def deltas(databases):
        result = one_changed_line(databases, "drift_generated_expression")
        finding = next(f for f in result.findings if f.key.path == COMPUTED)
        return result, {d.attribute: d for d in finding.deltas}

    def test_the_expression_is_shown_under_generated_and_not_under_default(self, databases):
        _, deltas = self.deltas(databases)
        assert "column.generated" in deltas
        assert "column.default" not in deltas, sorted(deltas)
        generated = deltas["column.generated"]
        assert "amount_scaled" in (generated.master_display or "")
        assert "amount_scaled" in (generated.target_display or "")

    def test_the_html_renders_the_generated_expression(self, databases):
        result, _ = self.deltas(databases)
        report = ComparisonReport(name="invoicing", master_label="prod", targets=(result,))
        out = io.StringIO()
        HtmlReporter().render(report, out)
        html = out.getvalue()
        assert "column.generated" in html
        assert "column.default" not in html, "no column in this fixture differs by its default"

    def test_a_serial_column_still_shows_its_nextval_default(self, databases):
        # The benign variant: the compared value is a sentinel, so the raw nextval(...) is the only
        # text a reader can see, and it is a real default.
        from db_schema_comparer.model.objects import SERIAL_SENTINEL
        from tests.integration.test_no_drift import inventory_of

        databases.setup("base")
        objects = {k.path: v for k, v in inventory_of(databases.master_dsn, "prod").objects.items()}
        column = objects[SERIAL]
        assert column.default == SERIAL_SENTINEL
        assert "nextval(" in (column.raw.get("default") or "")
        assert column.raw.get("generated") is None

    def test_the_servers_own_generation_expression_is_filed_under_generated(self, databases):
        from tests.integration.test_no_drift import inventory_of

        databases.setup("base")
        objects = {k.path: v for k, v in inventory_of(databases.master_dsn, "prod").objects.items()}
        column = objects[COMPUTED]
        assert column.default is None
        assert "amount_scaled" in (column.raw.get("generated") or "")
        assert column.raw.get("default") is None, "the slot a default would be read from"


LONG = "cumo-invoicing.column_flavours.computed_long"


class TestAGenerationExpressionIsNeverDiffedAsADefault:
    """The sharp case: one side generated, the other plainly defaulted, same column.

    ``column.default`` then really does differ — absent against ``7.5`` — so it gets a display pair,
    and with the expression filed under ``default`` that pair held a generation expression on a side
    whose compared default is empty. The fixture's expression is over 120 characters, which is what
    makes the renderer draw a diff of it rather than a one-line row.
    """

    @staticmethod
    def deltas(databases):
        databases.setup("base", drift="drift_generated_to_default")
        apply_sql(databases.master_dsn, "generated_long_master")
        result = compare(databases)
        finding = next(f for f in result.findings if f.key.path == LONG)
        return result, {d.attribute: d for d in finding.deltas}

    def test_the_default_delta_carries_no_generation_expression(self, databases):
        _, deltas = self.deltas(databases)
        default = deltas["column.default"]
        assert default.master_value is None and default.target_value is not None
        # Both or neither: the master has no default text at all, so there is nothing to show.
        assert default.master_display is None, default.master_display
        assert default.target_display is None
        assert _diff_for(default) == ()

    def test_the_generation_expression_is_on_the_generated_delta(self, databases):
        _, deltas = self.deltas(databases)
        generated = deltas["column.generated"]
        assert "amount_scaled" in (generated.master_value or "")
        assert generated.target_value is None

    def test_no_rendered_diff_anywhere_holds_the_expression_under_default(self, databases):
        result, _ = self.deltas(databases)
        report = ComparisonReport(name="invoicing", master_label="prod", targets=(result,))
        out = io.StringIO()
        HtmlReporter().render(report, out)
        html = out.getvalue()
        # Scoped to this delta's own region — up to the end of the finding's <dl> — because the
        # page also embeds the whole JSON payload, and because `column.generated` legitimately
        # renders a diff of the same expression a few lines further up.
        assert "<dt>column.default</dt>" in html
        region = html.split("<dt>column.default</dt>", 1)[1].split("</dl>", 1)[0]
        assert "amount_scaled" not in region, region[:400]
        assert 'class="diff"' not in region, region[:400]

    def test_the_payload_does_not_file_the_expression_under_default(self, databases):
        _, deltas = self.deltas(databases)
        payload = deltas["column.default"].to_json_dict()
        assert "master_display" not in payload, payload
