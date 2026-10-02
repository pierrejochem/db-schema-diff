"""What the detail pane shows for the selected finding.

Selection is an index into the *filtered* rows, so every test here pins the behaviour when the
filter moves underneath it: the pane describing a finding that is no longer listed is the bug this
interface is shaped to avoid. The diff must also agree with the HTML report's.
"""

from __future__ import annotations

from cumo_schema_comparer.report.html import MAX_DIFF_LINES
from cumo_schema_comparer.report.html import _diff_for as html_diff_for

from .conftest import _model, delta, differing, view_key


class TestDeltaRows:
    def test_each_delta_becomes_a_row(self, model_with_a_changed_view):
        rows = model_with_a_changed_view.delta_rows(0)
        assert [r["attribute"] for r in rows] == ["view.definition"]

    def test_every_field_is_present_on_every_row(self, model_with_a_changed_view):
        # Slint neither defaults nor rejects a partial row dict: a missing key is simply absent.
        expected = {"attribute", "master", "target", "severity", "note", "is_body"}
        for row in model_with_a_changed_view.delta_rows(0):
            assert set(row) == expected

    def test_a_none_value_becomes_an_empty_string_not_the_word_none(
        self, model_with_an_added_column
    ):
        rows = model_with_an_added_column.delta_rows(0)
        assert rows[0]["master"] == ""
        assert rows[0]["target"] == "0"

    def test_a_body_delta_is_flagged(self, model_with_a_changed_view):
        assert model_with_a_changed_view.delta_rows(0)[0]["is_body"] is True

    def test_a_scalar_delta_is_not(self, model_with_a_changed_column_type):
        assert model_with_a_changed_column_type.delta_rows(0)[0]["is_body"] is False


class TestDiffRows:
    def test_a_body_delta_produces_diff_lines(self, model_with_a_changed_view):
        kinds = {r["kind"] for r in model_with_a_changed_view.diff_rows(0)}
        assert "added" in kinds and "removed" in kinds

    def test_a_scalar_delta_produces_none(self, model_with_a_changed_column_type):
        assert model_with_a_changed_column_type.diff_rows(0) == []

    def test_every_field_is_present(self, model_with_a_changed_view):
        for row in model_with_a_changed_view.diff_rows(0):
            assert set(row) == {"kind", "text"}

    def test_master_only_lines_are_removed_and_target_only_lines_added(self):
        model = _model(
            differing(
                view_key(),
                delta("view.definition", "keep\nonly_master", "keep\nonly_target", body=True),
            )
        )
        rows = model.diff_rows(0)
        assert {"kind": "removed", "text": "only_master"} in rows
        assert {"kind": "added", "text": "only_target"} in rows
        assert {"kind": "added", "text": "only_master"} not in rows

    def test_a_non_body_delta_with_newlines_produces_no_diff(self):
        model = _model(differing(view_key(), delta("column.default", "a\nb", "a\nc")))
        assert model.diff_rows(0) == []

    def test_a_routine_diffs_its_display_text_not_its_hashes(self):
        model = _model(
            differing(
                view_key("fn"),
                delta(
                    "routine.body",
                    "a" * 64,
                    "b" * 64,
                    body=True,
                    master_display="select 1;\nselect 2;",
                    target_display="select 1;\nselect 3;",
                ),
            )
        )
        texts = [r["text"] for r in model.diff_rows(0)]
        assert "select 2;" in texts and "select 3;" in texts
        assert "a" * 64 not in texts
        row = model.delta_rows(0)[0]
        assert (row["master"], row["target"]) == ("a" * 64, "b" * 64)

    def test_a_routine_without_a_captured_body_renders_no_diff_but_shows_hashes(self):
        model = _model(
            differing(view_key("fn"), delta("routine.body", "a" * 64, "b" * 64, body=True))
        )
        assert model.diff_rows(0) == []
        assert model.delta_rows(0)[0]["master"] == "a" * 64

    def test_a_masked_pair_renders_normally(self):
        model = _model(
            differing(
                view_key(),
                delta(
                    "view.definition",
                    "select\n'***:a3f1c2'",
                    "select\n'***:b7c2d9'",
                    body=True,
                ),
            )
        )
        texts = [r["text"] for r in model.diff_rows(0)]
        assert "'***:a3f1c2'" in texts and "'***:b7c2d9'" in texts

    def test_it_agrees_with_the_html_report(self, model_with_a_changed_view):
        finding = model_with_a_changed_view._selected(0, needle="", severities=None)
        expected = [
            {"kind": line.kind.value, "text": line.text}
            for d in finding.deltas
            for line in html_diff_for(d)
        ]
        assert model_with_a_changed_view.diff_rows(0) == expected

    def test_a_long_diff_is_capped_at_the_html_limit(self):
        before = "\n".join(f"old{i}" for i in range(200))
        after = "\n".join(f"new{i}" for i in range(200))
        model = _model(differing(view_key(), delta("view.definition", before, after, body=True)))
        rows = model.diff_rows(0)
        assert len(rows) == MAX_DIFF_LINES + 1
        assert rows[-1]["kind"] == "elided"


class TestSelectionAgainstTheFilter:
    def test_an_out_of_range_index_is_empty_not_an_error(self, model_with_a_changed_view):
        assert model_with_a_changed_view.delta_rows(99) == []
        assert model_with_a_changed_view.diff_rows(99) == []

    def test_a_negative_index_is_empty(self, model_with_a_changed_view):
        assert model_with_a_changed_view.delta_rows(-1) == []
        assert model_with_a_changed_view.diff_rows(-1) == []

    def test_the_index_follows_the_filter(self, model_with_two_findings):
        """Index 0 under a filter must describe the filtered row, not the unfiltered first one."""
        unfiltered = model_with_two_findings.diff_rows(0)
        filtered = model_with_two_findings.diff_rows(0, needle="second")
        assert unfiltered != filtered
        assert {"kind": "added", "text": "z"} in filtered

    def test_the_filter_can_leave_nothing_to_select(self, model_with_two_findings):
        assert model_with_two_findings.delta_rows(0, needle="nope") == []
