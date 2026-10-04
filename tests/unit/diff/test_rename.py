"""Rename reconciliation.

PostgreSQL generates index, constraint and trigger names. Build the same unique constraint by two
slightly different migration paths and you get ``t_col_key`` here and ``t_col_key1`` there — one
structure, two names. Plain set-difference diffing calls that a missing object *and* an extra one,
and both are wrong.

``rename.py`` is generic over dataclasses, so these tests use a minimal stand-in rather than
waiting for the real index and constraint objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from db_schema_comparer.diff.model import ObjectFinding, ObjectStatus
from db_schema_comparer.diff.rename import reconcile_renames
from db_schema_comparer.diff.severity import Severity
from db_schema_comparer.model.keys import ObjectKey, table_key
from db_schema_comparer.model.kinds import ObjectKind
from db_schema_comparer.model.objects import RawValues


@dataclass(frozen=True, slots=True)
class FakeIndex:
    """Stands in for a real index object: a key, some structure, and display-only raw text."""

    key: ObjectKey
    columns: tuple[str, ...]
    unique: bool = False
    predicate: str | None = None
    raw: RawValues = field(default_factory=RawValues, compare=False, repr=False)


def index_key(name: str, schema: str = "public") -> ObjectKey:
    return ObjectKey(ObjectKind.INDEX, schema, name)


def missing(key: ObjectKey) -> ObjectFinding:
    return ObjectFinding(key=key, status=ObjectStatus.MISSING_IN_TARGET, severity=Severity.ERROR)


def extra(key: ObjectKey) -> ObjectFinding:
    return ObjectFinding(key=key, status=ObjectStatus.EXTRA_IN_TARGET, severity=Severity.WARNING)


class TestPairing:
    def test_identical_structure_under_two_names_becomes_one_finding(self):
        master_key, target_key = index_key("t_col_idx"), index_key("idx_t_col")
        result = reconcile_renames(
            [missing(master_key), extra(target_key)],
            {master_key: FakeIndex(master_key, ("col",))},
            {target_key: FakeIndex(target_key, ("col",))},
        )
        assert len(result) == 1
        finding = result[0]
        assert finding.status is ObjectStatus.DIFFERS
        # A generated name differing is not a missing index. Warning, not error.
        assert finding.severity is Severity.WARNING
        assert finding.paired_with == target_key
        assert [d.attribute for d in finding.deltas] == ["index.name"]
        assert (finding.deltas[0].master_value, finding.deltas[0].target_value) == (
            "t_col_idx",
            "idx_t_col",
        )

    @pytest.mark.parametrize(
        ("master_index", "target_index"),
        [
            (FakeIndex(index_key("a"), ("col",)), FakeIndex(index_key("b"), ("other",))),
            (FakeIndex(index_key("a"), ("col",), unique=True), FakeIndex(index_key("b"), ("col",))),
            (
                FakeIndex(index_key("a"), ("col",), predicate="x > 0"),
                FakeIndex(index_key("b"), ("col",)),
            ),
        ],
    )
    def test_different_structures_are_not_paired(self, master_index, target_index):
        result = reconcile_renames(
            [missing(master_index.key), extra(target_index.key)],
            {master_index.key: master_index},
            {target_index.key: target_index},
        )
        assert {f.status for f in result} == {
            ObjectStatus.MISSING_IN_TARGET,
            ObjectStatus.EXTRA_IN_TARGET,
        }

    def test_a_different_schema_prevents_pairing(self):
        master_key = index_key("t_col_idx", "public")
        target_key = index_key("t_col_idx2", "other")
        result = reconcile_renames(
            [missing(master_key), extra(target_key)],
            {master_key: FakeIndex(master_key, ("col",))},
            {target_key: FakeIndex(target_key, ("col",))},
        )
        assert len(result) == 2

    def test_raw_text_does_not_affect_pairing(self):
        # raw is display-only, so two servers printing the same index differently still pair.
        master_key, target_key = index_key("a"), index_key("b")
        result = reconcile_renames(
            [missing(master_key), extra(target_key)],
            {
                master_key: FakeIndex(
                    master_key, ("col",), raw=RawValues({"definition": "CREATE ..."})
                )
            },
            {
                target_key: FakeIndex(
                    target_key, ("col",), raw=RawValues({"definition": "create ..."})
                )
            },
        )
        assert len(result) == 1


class TestScope:
    def test_only_auto_named_kinds_participate(self):
        # A renamed table is genuinely a missing table plus an extra one: nothing generated
        # that name, so somebody chose it deliberately.
        master_key, target_key = table_key("public", "invoice"), table_key("public", "invoices")
        findings = [missing(master_key), extra(target_key)]
        result = reconcile_renames(findings, {}, {})
        assert result == findings

    def test_findings_that_are_neither_missing_nor_extra_pass_through(self):
        key = index_key("t_col_idx")
        differs = ObjectFinding(key=key, status=ObjectStatus.DIFFERS, severity=Severity.WARNING)
        assert reconcile_renames([differs], {}, {}) == [differs]

    def test_nothing_happens_without_both_sides(self):
        key = index_key("only_missing")
        findings = [missing(key)]
        assert reconcile_renames(findings, {key: FakeIndex(key, ("c",))}, {}) == findings


class TestMultiplePairs:
    def test_each_pair_is_matched_once(self):
        m1, m2 = index_key("m_one"), index_key("m_two")
        t1, t2 = index_key("t_one"), index_key("t_two")
        master = {m1: FakeIndex(m1, ("a",)), m2: FakeIndex(m2, ("b",))}
        target = {t1: FakeIndex(t1, ("a",)), t2: FakeIndex(t2, ("b",))}
        result = reconcile_renames([missing(m1), missing(m2), extra(t1), extra(t2)], master, target)
        assert len(result) == 2
        assert all(f.status is ObjectStatus.DIFFERS for f in result)
        assert {f.paired_with for f in result} == {t1, t2}

    def test_an_unpaired_leftover_keeps_its_original_status(self):
        m1, m2 = index_key("m_one"), index_key("m_two")
        t1 = index_key("t_one")
        master = {m1: FakeIndex(m1, ("a",)), m2: FakeIndex(m2, ("unmatched",))}
        target = {t1: FakeIndex(t1, ("a",))}
        result = reconcile_renames([missing(m1), missing(m2), extra(t1)], master, target)
        by_status = {f.status for f in result}
        assert by_status == {ObjectStatus.DIFFERS, ObjectStatus.MISSING_IN_TARGET}

    def test_pairing_is_deterministic_when_two_candidates_are_identical(self):
        # Two structurally identical extras: whichever is chosen, it must be the same one on
        # every run, or consecutive reports would differ for no reason.
        m1 = index_key("m_one")
        t_a, t_b = index_key("t_aaa"), index_key("t_bbb")
        master = {m1: FakeIndex(m1, ("a",))}
        target = {t_a: FakeIndex(t_a, ("a",)), t_b: FakeIndex(t_b, ("a",))}
        results = {
            reconcile_renames([missing(m1), extra(t_a), extra(t_b)], master, target)[0].paired_with
            for _ in range(5)
        }
        assert len(results) == 1


@dataclass(frozen=True, slots=True)
class FakeConstraint:
    """Stands in for a real constraint: its key carries the table *and* its own generated name."""

    key: ObjectKey
    contype: str = "u"
    columns: tuple[str, ...] = ()
    raw: RawValues = field(default_factory=RawValues, compare=False, repr=False)


def constraint_key(table: str, name: str, schema: str = "public") -> ObjectKey:
    return ObjectKey(ObjectKind.CONSTRAINT, schema, table, name)


class TestKeyShapeAwareness:
    """Which part of the key is the generated name depends on the kind.

    An index's key is ``(schema, index_name)``. A constraint's is ``(schema, table, own_name)``, so
    its ``name`` is the *table* and is structural. Treating both the same way silently pairs
    constraints that belong to different tables.
    """

    def test_two_identically_shaped_constraints_on_the_same_table_pair(self):
        master_key = constraint_key("invoice", "invoice_number_key")
        target_key = constraint_key("invoice", "invoice_number_key1")
        result = reconcile_renames(
            [missing(master_key), extra(target_key)],
            {master_key: FakeConstraint(master_key, columns=("number",))},
            {target_key: FakeConstraint(target_key, columns=("number",))},
        )
        assert len(result) == 1
        assert result[0].status is ObjectStatus.DIFFERS
        assert [d.attribute for d in result[0].deltas] == ["constraint.name"]

    def test_identically_shaped_constraints_on_different_tables_do_not_pair(self):
        # Both are UNIQUE(number), but on different tables. Pairing them would report a real
        # missing constraint as a harmless rename.
        master_key = constraint_key("invoice", "invoice_number_key")
        target_key = constraint_key("credit_note", "credit_note_number_key")
        result = reconcile_renames(
            [missing(master_key), extra(target_key)],
            {master_key: FakeConstraint(master_key, columns=("number",))},
            {target_key: FakeConstraint(target_key, columns=("number",))},
        )
        assert {f.status for f in result} == {
            ObjectStatus.MISSING_IN_TARGET,
            ObjectStatus.EXTRA_IN_TARGET,
        }

    def test_constraints_of_different_types_do_not_pair(self):
        master_key = constraint_key("invoice", "invoice_pkey")
        target_key = constraint_key("invoice", "invoice_number_key")
        result = reconcile_renames(
            [missing(master_key), extra(target_key)],
            {master_key: FakeConstraint(master_key, contype="p", columns=("id",))},
            {target_key: FakeConstraint(target_key, contype="u", columns=("id",))},
        )
        assert len(result) == 2

    def test_an_index_on_a_different_table_does_not_pair(self):
        # For an index the table is a compared field rather than part of the key, so this has to
        # be blocked by the fingerprint's field walk instead.
        master_key, target_key = index_key("a_idx"), index_key("b_idx")
        result = reconcile_renames(
            [missing(master_key), extra(target_key)],
            {master_key: FakeIndexWithTable(master_key, "invoice", ("number",))},
            {target_key: FakeIndexWithTable(target_key, "credit_note", ("number",))},
        )
        assert len(result) == 2


@dataclass(frozen=True, slots=True)
class FakeIndexWithTable:
    key: ObjectKey
    table: str
    columns: tuple[str, ...]
    raw: RawValues = field(default_factory=RawValues, compare=False, repr=False)
