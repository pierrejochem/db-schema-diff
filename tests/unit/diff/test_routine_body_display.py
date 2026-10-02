"""A differing routine carries its body text for display; the hash still decides.

The guarantee under test is that nothing about *comparison* changed: a layout-only difference
canonicalises to the same hash and so produces no finding, and a version 1 capture (no body text)
against a fresh one is compared by hash exactly as before.
"""

from cumo_schema_comparer.baseline import signature
from cumo_schema_comparer.diff.engine import diff_inventories
from cumo_schema_comparer.diff.model import AttributeDelta, DiffOptions
from cumo_schema_comparer.diff.severity import Severity
from cumo_schema_comparer.model.objects import RawValues
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


HASH_ATTRIBUTE = "routine.body_hash"


def is_hash(value):
    return value is not None and len(value) == 64


class TestTextCarriedWhenBothSidesHaveIt:
    def test_differing_bodies_keep_the_hashes_and_add_the_text(self):
        result = diff(routine("public", SIGNATURE, OLD), routine("public", SIGNATURE, NEW))

        (delta,) = deltas(result)
        assert delta.attribute == HASH_ATTRIBUTE
        assert is_hash(delta.master_value) and is_hash(delta.target_value)
        assert delta.master_display == "BEGIN RETURN a + b ; END"
        assert delta.target_display == "BEGIN RETURN a - b ; END"
        assert delta.severity is Severity.WARNING

    def test_a_layout_only_difference_is_no_finding_at_all(self):
        # The semantics-unchanged guarantee: the canonical hashes are equal, so nothing differs.
        reflowed = "BEGIN\n\n      RETURN   a + b;   -- add them\nEND\n"
        result = diff(routine("public", SIGNATURE, OLD), routine("public", SIGNATURE, reflowed))

        assert result.findings == ()
        assert result.worst_severity() is None

    def test_a_layout_only_difference_is_not_even_cosmetic(self):
        reflowed = "BEGIN\n\n      RETURN   a + b;   -- add them\nEND\n"
        result = diff(
            routine("public", SIGNATURE, OLD, raw=RawValues({"body": OLD})),
            routine("public", SIGNATURE, reflowed, raw=RawValues({"body": reflowed})),
            options=DiffOptions(show_cosmetic=True),
        )

        assert result.findings == ()

    def test_identical_routines_are_no_finding(self):
        result = diff(routine("public", SIGNATURE, OLD), routine("public", SIGNATURE, OLD))

        assert result.findings == ()


class TestEmptyBodyIsABody:
    """An empty string is a real body (a comment-only routine), so absence means ``None`` only."""

    def test_empty_master_against_text(self):
        result = diff(routine("public", SIGNATURE, "-- nothing"), routine("public", SIGNATURE, NEW))

        (delta,) = deltas(result)
        assert (delta.master_display, delta.target_display) == ("", "BEGIN RETURN a - b ; END")

    def test_text_against_empty_target(self):
        result = diff(routine("public", SIGNATURE, OLD), routine("public", SIGNATURE, "-- nothing"))

        (delta,) = deltas(result)
        assert (delta.master_display, delta.target_display) == ("BEGIN RETURN a + b ; END", "")

    def test_both_empty_but_hashes_forced_apart(self):
        result = diff(
            routine("public", SIGNATURE, "-- a", body_hash="h1"),
            routine("public", SIGNATURE, "-- b", body_hash="h2"),
        )

        (delta,) = deltas(result)
        assert (delta.master_display, delta.target_display) == ("", "")


class TestHashesOnlyOtherwise:
    def test_a_version_1_master_has_no_display_values(self):
        result = diff(
            routine("public", SIGNATURE, OLD, body=None), routine("public", SIGNATURE, NEW)
        )

        (delta,) = deltas(result)
        assert delta.attribute == HASH_ATTRIBUTE
        assert is_hash(delta.master_value) and is_hash(delta.target_value)
        assert (delta.master_display, delta.target_display) == (None, None)

    def test_a_version_1_target_has_no_display_values(self):
        result = diff(
            routine("public", SIGNATURE, OLD), routine("public", SIGNATURE, NEW, body=None)
        )

        (delta,) = deltas(result)
        assert delta.attribute == HASH_ATTRIBUTE
        assert (delta.master_display, delta.target_display) == (None, None)

    def test_neither_side_having_a_body(self):
        result = diff(
            routine("public", SIGNATURE, OLD, body=None),
            routine("public", SIGNATURE, NEW, body=None),
        )

        (delta,) = deltas(result)
        assert is_hash(delta.master_value)
        assert (delta.master_display, delta.target_display) == (None, None)

    def test_a_version_1_capture_of_the_same_routine_is_not_a_difference(self):
        # The upgrade path: an old capture has no body text, but the hash still matches.
        result = diff(
            routine("public", SIGNATURE, OLD, body=None), routine("public", SIGNATURE, OLD)
        )

        assert result.findings == ()


class TestBaselineIdentityUnchanged:
    def test_the_signature_uses_the_attribute_and_hashes_not_the_text(self):
        master = routine("public", SIGNATURE, OLD)
        target = routine("public", SIGNATURE, NEW)
        result = diff(master, target)
        (finding,) = result.findings

        *_, deltas_part = signature("svc", "qa", finding)

        (master_obj,) = master.values()
        (target_obj,) = target.values()
        assert deltas_part == ((HASH_ATTRIBUTE, master_obj.body_hash, target_obj.body_hash),)
        assert "BEGIN" not in repr(deltas_part)

    def test_the_signature_is_the_same_with_or_without_body_text(self):
        with_text = diff(routine("public", SIGNATURE, OLD), routine("public", SIGNATURE, NEW))
        v1 = diff(
            routine("public", SIGNATURE, OLD, body=None),
            routine("public", SIGNATURE, NEW, body=None),
        )

        assert signature("svc", "qa", with_text.findings[0]) == signature(
            "svc", "qa", v1.findings[0]
        )

    def test_the_json_round_trips_and_omits_display_when_absent(self):
        v1 = diff(
            routine("public", SIGNATURE, OLD, body=None),
            routine("public", SIGNATURE, NEW, body=None),
        )
        payload = deltas(v1)[0].to_json_dict()
        assert "master_display" not in payload
        full = deltas(diff(routine("public", SIGNATURE, OLD), routine("public", SIGNATURE, NEW)))[0]
        assert AttributeDelta.from_json_dict(full.to_json_dict()) == full


class TestSeverity:
    def test_a_major_version_skew_downgrades_the_difference_to_info(self):
        result = diff(
            routine("public", SIGNATURE, OLD),
            routine("public", SIGNATURE, NEW),
            master_version=150004,
            version=170002,
        )

        (delta,) = deltas(result)
        assert delta.attribute == HASH_ATTRIBUTE
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
