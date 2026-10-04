"""What the detail pane shows for the selected finding.

Selection is an index into the *filtered* rows, so every test here pins the behaviour when the
filter moves underneath it: the pane describing a finding that is no longer listed is the bug this
interface is shaped to avoid. The diff must also agree with the HTML report's.
"""

from __future__ import annotations

from db_schema_diff.diff.model import ObjectFinding, ObjectStatus
from db_schema_diff.diff.severity import Severity
from db_schema_diff.report.html import MAX_DIFF_LINES
from db_schema_diff.report.html import _diff_for as html_diff_for

from .conftest import _model, _model_of, delta, differing, view_key


def ident(model, position=0, **filters):
    """The (target, path) of the row at ``position`` in the filtered list."""
    row = model.finding_rows(**filters)[position]
    return row.target, row.kind, row.path


class TestDeltaRows:
    def test_each_delta_becomes_a_row(self, model_with_a_changed_view):
        rows = model_with_a_changed_view.delta_rows(*ident(model_with_a_changed_view))
        assert [r["attribute"] for r in rows] == ["view.definition"]

    def test_every_field_is_present_on_every_row(self, model_with_a_changed_view):
        # Slint neither defaults nor rejects a partial row dict: a missing key is simply absent.
        expected = {"attribute", "master", "target", "severity", "note", "is_body"}
        for row in model_with_a_changed_view.delta_rows(*ident(model_with_a_changed_view)):
            assert set(row) == expected

    def test_a_none_value_becomes_an_empty_string_not_the_word_none(
        self, model_with_an_added_column
    ):
        rows = model_with_an_added_column.delta_rows(*ident(model_with_an_added_column))
        assert rows[0]["master"] == ""
        assert rows[0]["target"] == "0"

    def test_a_body_delta_is_flagged(self, model_with_a_changed_view):
        assert (
            model_with_a_changed_view.delta_rows(*ident(model_with_a_changed_view))[0]["is_body"]
            is True
        )

    def test_a_scalar_delta_is_not(self, model_with_a_changed_column_type):
        assert (
            model_with_a_changed_column_type.delta_rows(*ident(model_with_a_changed_column_type))[
                0
            ]["is_body"]
            is False
        )


class TestDiffRows:
    def test_a_body_delta_produces_diff_lines(self, model_with_a_changed_view):
        kinds = {
            r["kind"]
            for r in model_with_a_changed_view.diff_rows(*ident(model_with_a_changed_view))
        }
        assert "added" in kinds and "removed" in kinds

    def test_a_scalar_delta_produces_none(self, model_with_a_changed_column_type):
        assert (
            model_with_a_changed_column_type.diff_rows(*ident(model_with_a_changed_column_type))
            == []
        )

    def test_every_field_is_present(self, model_with_a_changed_view):
        for row in model_with_a_changed_view.diff_rows(*ident(model_with_a_changed_view)):
            assert set(row) == {"attribute", "kind", "text"}

    def test_master_only_lines_are_removed_and_target_only_lines_added(self):
        model = _model(
            differing(
                view_key(),
                delta("view.definition", "keep\nonly_master", "keep\nonly_target", body=True),
            )
        )
        rows = model.diff_rows(*ident(model))
        assert {"attribute": "view.definition", "kind": "removed", "text": "only_master"} in rows
        assert {"attribute": "view.definition", "kind": "added", "text": "only_target"} in rows
        assert all(r["text"] != "only_master" or r["kind"] == "removed" for r in rows)

    def test_a_non_body_delta_with_newlines_produces_no_diff(self):
        model = _model(differing(view_key(), delta("column.default", "a\nb", "a\nc")))
        assert model.diff_rows(*ident(model)) == []

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
        texts = [r["text"] for r in model.diff_rows(*ident(model))]
        assert "select 2;" in texts and "select 3;" in texts
        assert "a" * 64 not in texts
        row = model.delta_rows(*ident(model))[0]
        assert (row["master"], row["target"]) == ("a" * 64, "b" * 64)

    def test_a_routine_without_a_captured_body_renders_no_diff_but_shows_hashes(self):
        model = _model(
            differing(view_key("fn"), delta("routine.body", "a" * 64, "b" * 64, body=True))
        )
        assert model.diff_rows(*ident(model)) == []
        assert model.delta_rows(*ident(model))[0]["master"] == "a" * 64

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
        texts = [r["text"] for r in model.diff_rows(*ident(model))]
        assert "'***:a3f1c2'" in texts and "'***:b7c2d9'" in texts

    def test_it_agrees_with_the_html_report(self, model_with_a_changed_view):
        finding = model_with_a_changed_view._selected(
            *ident(model_with_a_changed_view), needle="", severities=None
        )
        expected = [
            {"attribute": d.attribute, "kind": line.kind.value, "text": line.text}
            for d in finding.deltas
            for line in html_diff_for(d)
        ]
        assert model_with_a_changed_view.diff_rows(*ident(model_with_a_changed_view)) == expected

    def test_a_long_diff_is_capped_at_the_html_limit(self):
        before = "\n".join(f"old{i}" for i in range(200))
        after = "\n".join(f"new{i}" for i in range(200))
        model = _model(differing(view_key(), delta("view.definition", before, after, body=True)))
        rows = model.diff_rows(*ident(model))
        assert len(rows) == MAX_DIFF_LINES + 1
        assert rows[-1]["kind"] == "elided"


class TestMoreShapes:
    def test_each_body_delta_is_labelled_and_grouped_in_delta_order(self):
        model = _model(
            differing(
                view_key(),
                delta("check.one", "a\nb", "a\nc", body=True),
                delta("check.two", "x\ny", "x\nz", body=True),
            )
        )
        rows = model.diff_rows(*ident(model))
        assert all(set(r) == {"attribute", "kind", "text"} for r in rows)
        labels = [r["attribute"] for r in rows]
        assert set(labels) == {"check.one", "check.two"}
        assert labels == sorted(labels)  # one then two: grouped, never interleaved
        assert labels.index("check.two") == labels.count("check.one")
        assert [r["attribute"] for r in model.delta_rows(*ident(model))] == [
            "check.one",
            "check.two",
        ]

    def test_a_none_note_is_an_empty_string(self, model_with_a_changed_view):
        assert (
            model_with_a_changed_view.delta_rows(*ident(model_with_a_changed_view))[0]["note"] == ""
        )

    def test_a_note_keeps_its_value(self):
        model = _model(differing(view_key(), delta("column.data_type", "a", "b", note="why")))
        assert model.delta_rows(*ident(model))[0]["note"] == "why"

    def test_a_suppressed_finding_resolves(self):
        ignored = ObjectFinding(
            key=view_key("quiet"),
            status=ObjectStatus.DIFFERS,
            severity=Severity.WARNING,
            deltas=(delta("view.definition", "a\nb", "a\nc", body=True),),
            ignored_by="rule-1",
        )
        model = _model(ignored=(ignored,))
        assert model.finding_rows()[0].suppressed_by == "rule-1"
        assert [r["attribute"] for r in model.delta_rows(*ident(model))] == ["view.definition"]
        assert model.diff_rows(*ident(model)) != []

    def test_an_empty_severity_set_hides_everything_but_none_hides_nothing(
        self, model_with_a_changed_view
    ):
        who = ident(model_with_a_changed_view)
        assert model_with_a_changed_view.delta_rows(*who, severities=frozenset()) == []
        assert model_with_a_changed_view.diff_rows(*who, severities=frozenset()) == []
        assert model_with_a_changed_view.delta_rows(*who, severities=None) != []
        assert model_with_a_changed_view.diff_rows(*who, severities=None) != []


class TestSelectionByIdentity:
    def test_a_finding_that_moved_still_resolves_to_itself(self, model_with_two_findings):
        beta = ident(model_with_two_findings, 1)
        before = model_with_two_findings.diff_rows(*beta)
        assert before != []
        # Under this needle beta is index 0; under none it is index 1. Same detail either way.
        needle = beta[2].split(".")[-1]
        assert ident(model_with_two_findings, 0, needle=needle) == beta
        assert model_with_two_findings.diff_rows(*beta, needle=needle) == before
        assert model_with_two_findings.delta_rows(*beta, needle=needle) == (
            model_with_two_findings.delta_rows(*beta)
        )

    def test_a_filtered_out_identity_is_empty(self, model_with_two_findings):
        first = ident(model_with_two_findings, 0)
        second_needle = ident(model_with_two_findings, 1)[2].split(".")[-1]
        assert model_with_two_findings.delta_rows(*first, needle=second_needle) == []
        assert model_with_two_findings.diff_rows(*first, needle=second_needle) == []

    def test_an_unknown_target_or_path_is_empty(self, model_with_a_changed_view):
        target, kind, path = ident(model_with_a_changed_view)
        assert model_with_a_changed_view.delta_rows("nope", kind, path) == []
        assert model_with_a_changed_view.delta_rows(target, kind, "nope") == []
        assert model_with_a_changed_view.diff_rows("nope", kind, path) == []
        assert model_with_a_changed_view.diff_rows(target, kind, "nope") == []

    def test_the_same_path_on_two_targets_resolves_to_each(self):
        def finding(master, target):
            return differing(view_key(), delta("view.definition", master, target, body=True))

        model = _model_of(
            {
                "qa": ((finding("a\nb", "a\nQA"),), ()),
                "dev": ((finding("a\nb", "a\nDEV"),), ()),
            }
        )
        kind, path = model.finding_rows()[0].kind, model.finding_rows()[0].path
        qa = [r["text"] for r in model.diff_rows("qa", kind, path)]
        dev = [r["text"] for r in model.diff_rows("dev", kind, path)]
        assert "QA" in qa and "DEV" not in qa
        assert "DEV" in dev and "QA" not in dev


class TestKindsSharingAPath:
    """A column, a constraint and a trigger on one table share ``schema.table.name``."""

    @staticmethod
    def _model():
        from db_schema_diff.model.keys import ObjectKey
        from db_schema_diff.model.kinds import ObjectKind

        def finding(kind, marker):
            return differing(
                ObjectKey(kind, "public", "t", "x"),
                delta("view.definition", f"same\n{marker}1", f"same\n{marker}2", body=True),
            )

        return _model(
            finding(ObjectKind.COLUMN, "col"),
            finding(ObjectKind.CONSTRAINT, "con"),
            finding(ObjectKind.TRIGGER, "trg"),
        )

    def test_the_fixture_really_collides(self):
        rows = self._model().finding_rows()
        assert len({r.path for r in rows}) == 1
        assert {r.kind for r in rows} == {"column", "constraint", "trigger"}

    def test_each_identity_resolves_to_its_own_deltas_and_diff(self):
        model = self._model()
        for row in model.finding_rows():
            marker = {"column": "col", "constraint": "con", "trigger": "trg"}[row.kind]
            texts = {
                r["text"]
                for r in model.diff_rows(row.target, row.kind, row.path)
                if r["kind"] != "hunk"
            }
            assert texts == {"same", f"{marker}1", f"{marker}2"}
            assert model.delta_rows(row.target, row.kind, row.path)[0]["master"].endswith(
                f"{marker}1"
            )

    def test_every_row_round_trips_under_several_filters(self, model_with_two_findings):
        for model in (self._model(), model_with_two_findings):
            for filters in (
                {},
                {"needle": "x"},
                {"needle": "second"},
                {"severities": frozenset({"error"})},
                {"severities": None},
            ):
                for row in model.finding_rows(**filters):
                    got = model.delta_rows(row.target, row.kind, row.path, **filters)
                    assert [d["attribute"] for d in got] != []


# --- The row guard: a missing key aborts the process in Slint, so Python rejects it first. ---

import pytest as _pytest  # noqa: E402

from db_schema_diff.gui.errors import GuiError as _GuiError  # noqa: E402
from db_schema_diff.gui.results_vm import (  # noqa: E402
    DELTA_ROW_FIELDS,
    DIFF_ROW_FIELDS,
    checked_delta_rows,
    checked_diff_rows,
)

_GUARDS = [
    (checked_delta_rows, DELTA_ROW_FIELDS),
    (checked_diff_rows, DIFF_ROW_FIELDS),
]


def _full(fields):
    return {name: ("" if name != "is_body" else False) for name in fields}


@_pytest.mark.parametrize(("guard", "fields"), _GUARDS)
def test_a_complete_row_passes_and_an_empty_sequence_passes(guard, fields):
    assert guard([_full(fields), _full(fields)]) == [_full(fields), _full(fields)]
    assert guard([]) == []


@_pytest.mark.parametrize(("guard", "fields"), _GUARDS)
def test_each_missing_field_raises_naming_field_and_row(guard, fields):
    for name in fields:
        broken = _full(fields)
        del broken[name]
        with _pytest.raises(_GuiError) as caught:
            guard([_full(fields), broken])
        assert f"'{name}'" in str(caught.value)
        assert "row 1" in str(caught.value)


@_pytest.mark.parametrize(("guard", "fields"), _GUARDS)
def test_an_empty_dict_raises(guard, fields):
    with _pytest.raises(_GuiError):
        guard([{}])
