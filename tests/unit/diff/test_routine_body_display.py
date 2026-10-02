"""A differing routine shows its body text, while the body hash still decides that it differs.

The guarantee under test is that nothing about *comparison* changed: a layout-only difference
canonicalises to the same hash and so produces no finding, and a version 1 capture (no body text)
against a fresh one is compared by hash exactly as before.
"""

from cumo_schema_comparer.diff.engine import diff_inventories
from cumo_schema_comparer.diff.model import DiffOptions
from cumo_schema_comparer.diff.severity import Severity
from tests.support.builders import col, inventory, routine, table

SIGNATURE = "f_add(integer, integer)"
OLD = "BEGIN\n  RETURN a + b;\nEND"
NEW = "BEGIN\n  RETURN a - b;\nEND"


def keep():
    return table("public", "keep", cols=[col("k")])


def diff(master_routine, target_routine, *, options=None, master_version=150004, **target_kwargs):
    return diff_inventories(
        inventory(keep(), master_routine, label="prod", version=master_version),
        inventory(keep(), target_routine, label="qa", **target_kwargs),
        options=options,
    )


def deltas(result):
    return [d for f in result.findings for d in f.deltas]


class TestTextShownWhenBothSidesHaveIt:
    def test_differing_bodies_show_the_text_not_the_hashes(self):
        result = diff(routine("public", SIGNATURE, OLD), routine("public", SIGNATURE, NEW))

        (delta,) = deltas(result)
        assert delta.attribute == "routine.body"
        assert delta.master_value == "BEGIN RETURN a + b ; END"
        assert delta.target_value == "BEGIN RETURN a - b ; END"
        assert delta.severity is Severity.WARNING

    def test_a_layout_only_difference_is_no_finding_at_all(self):
        # The semantics-unchanged guarantee: the canonical hashes are equal, so nothing differs.
        reflowed = "BEGIN\n\n      RETURN   a + b;   -- add them\nEND\n"
        result = diff(routine("public", SIGNATURE, OLD), routine("public", SIGNATURE, reflowed))

        assert result.findings == ()
        assert result.worst_severity() is None

    def test_identical_routines_are_no_finding(self):
        result = diff(routine("public", SIGNATURE, OLD), routine("public", SIGNATURE, OLD))

        assert result.findings == ()


class TestHashesShownOtherwise:
    def test_a_version_1_master_shows_hashes(self):
        result = diff(
            routine("public", SIGNATURE, OLD, body=None), routine("public", SIGNATURE, NEW)
        )

        (delta,) = deltas(result)
        assert delta.attribute == "routine.body"
        assert delta.master_value is not None and len(delta.master_value) == 64
        assert delta.target_value is not None and len(delta.target_value) == 64

    def test_a_version_1_target_shows_hashes(self):
        result = diff(
            routine("public", SIGNATURE, OLD), routine("public", SIGNATURE, NEW, body=None)
        )

        (delta,) = deltas(result)
        assert delta.attribute == "routine.body"
        assert delta.master_value is not None and len(delta.master_value) == 64
        assert delta.target_value is not None and len(delta.target_value) == 64

    def test_neither_side_having_a_body_shows_hashes(self):
        result = diff(
            routine("public", SIGNATURE, OLD, body=None),
            routine("public", SIGNATURE, NEW, body=None),
        )

        (delta,) = deltas(result)
        assert delta.attribute == "routine.body"
        assert delta.master_value is not None and len(delta.master_value) == 64

    def test_a_version_1_capture_of_the_same_routine_is_not_a_difference(self):
        # The upgrade path: an old capture has no body text, but the hash still matches.
        result = diff(
            routine("public", SIGNATURE, OLD, body=None), routine("public", SIGNATURE, OLD)
        )

        assert result.findings == ()


class TestSeverity:
    def test_a_major_version_skew_downgrades_the_difference_to_info(self):
        result = diff(
            routine("public", SIGNATURE, OLD),
            routine("public", SIGNATURE, NEW),
            master_version=150004,
            version=170002,
        )

        (delta,) = deltas(result)
        assert delta.attribute == "routine.body"
        assert delta.severity is Severity.INFO

    def test_downgrade_bodies_set_directly_gives_info(self):
        result = diff(
            routine("public", SIGNATURE, OLD),
            routine("public", SIGNATURE, NEW),
            options=DiffOptions(downgrade_bodies=True),
        )

        (delta,) = deltas(result)
        assert delta.severity is Severity.INFO


class TestOtherAttributesUnaffected:
    def test_a_spec_without_a_display_getter_shows_the_compared_values(self):
        result = diff(
            table("public", "invoice", cols=[col("id", "int4")]),
            table("public", "invoice", cols=[col("id", "int8")]),
        )

        (delta,) = [d for d in deltas(result) if d.attribute == "column.data_type"]
        assert (delta.master_value, delta.target_value) == ("int4", "int8")
