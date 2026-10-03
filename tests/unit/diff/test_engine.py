"""The diff engine.

Pure function of two inventories, so every case here is two small hand-built inventories that
differ in exactly one way, plus an assertion about what should be reported and at what severity.
"""

import pytest

from cumo_schema_comparer.diff.engine import diff_inventories
from cumo_schema_comparer.diff.model import DiffOptions, NoteKind, ObjectStatus
from cumo_schema_comparer.diff.severity import Severity
from cumo_schema_comparer.model.objects import SERIAL_SENTINEL
from tests.support.builders import col, inventory, table


def findings_by_path(diff):
    return {f.key.path: f for f in diff.findings}


def statuses(diff):
    return {(f.key.path, f.status) for f in diff.findings}


#: A table present in both sides, so a target is never *entirely* empty and the
#: empty-target short circuit does not swallow the case under test.
def shared():
    return table("public", "keep", cols=[col("k")])


def diff(master_objects, target_objects, **kwargs):
    options = kwargs.pop("options", None)
    return diff_inventories(
        inventory(master_objects, label="prod", **kwargs),
        inventory(target_objects, label="qa", **kwargs),
        options=options,
    )


class TestInSync:
    def test_identical_inventories_produce_no_findings(self):
        objects = table(
            "public",
            "invoice",
            cols=[col("id", "int4", nullable=False), col("amount", "numeric(10,2)")],
        )
        result = diff(objects, objects)
        assert result.findings == ()
        assert result.worst_severity() is None

    def test_an_empty_master_and_empty_target_is_in_sync(self):
        assert diff({}, {}).findings == ()


class TestExistence:
    def test_a_table_missing_from_the_target_is_an_error(self):
        result = diff(shared() | table("public", "invoice"), shared())
        finding = findings_by_path(result)["public.invoice"]
        assert finding.status is ObjectStatus.MISSING_IN_TARGET
        # The master is authoritative: a missing object means a migration did not land.
        assert finding.severity is Severity.ERROR

    def test_an_extra_table_in_the_target_is_a_warning(self):
        result = diff(shared(), shared() | table("public", "scratch"))
        finding = findings_by_path(result)["public.scratch"]
        assert finding.status is ObjectStatus.EXTRA_IN_TARGET
        # Usually someone experimenting in a lower environment, not a broken deployment.
        assert finding.severity is Severity.WARNING

    def test_a_missing_column_is_reported_independently_of_its_table(self):
        master = table("public", "invoice", cols=[col("id", "int4"), col("note", "text")])
        target = table("public", "invoice", cols=[col("id", "int4")])
        result = diff(master, target)
        assert ("public.invoice.note", ObjectStatus.MISSING_IN_TARGET) in statuses(result)
        assert "public.invoice" not in findings_by_path(result)


class TestEmptyTarget:
    def test_an_empty_target_collapses_to_a_single_note(self):
        master = table("public", "a", cols=[col("x")]) | table("public", "b", cols=[col("y")])
        result = diff(master, {})
        # Four objects missing would be four findings; one deployment never happened is one fact.
        assert result.findings == ()
        assert [n.kind for n in result.notes] == [NoteKind.TARGET_EMPTY]
        assert result.worst_severity() is Severity.ERROR

    def test_an_empty_master_is_not_treated_as_an_empty_target(self):
        """And not as "nothing compared" either: everything in the target is extra, which is a
        loud result rather than a silent one."""
        result = diff({}, table("public", "scratch"))
        assert [n.kind for n in result.notes] == []
        assert result.findings


class TestNothingCompared:
    """Two empty sides used to report "No differences found" — the worst answer this tool can give.

    The guard above only fired when the *target* alone was empty, so a comparison that inspected
    nothing at all fell through it and came back clean, at exit 0. The commonest way to arrive
    there is a ``schemas:`` filter that matches no schema: a typo, or a schema renamed per
    environment with no ``schema_map``. Every count reads zero and the verdict reads green.
    """

    def test_two_empty_sides_are_an_error_not_a_clean_run(self):
        result = diff({}, {})
        assert [n.kind for n in result.notes] == [NoteKind.NOTHING_COMPARED]
        assert result.worst_severity() is Severity.ERROR

    def test_the_note_says_what_to_go_and_look_at(self):
        """A bare "nothing compared" sends nobody anywhere. The schema filter is the usual cause."""
        ((note,)) = diff({}, {}).notes
        assert "schema" in note.message

    def test_it_fails_a_gate_rather_than_passing_one(self):
        """The whole point: a scripted run must not come back 0 having looked at nothing."""
        assert diff({}, {}).worst_severity() >= Severity.ERROR

    def test_an_extension_on_both_sides_is_still_nothing_compared(self):
        """``plpgsql`` exists in every database, so counting it would make an empty schema look
        inspected and the guard would never fire."""
        from cumo_schema_comparer.model.keys import ObjectKey
        from cumo_schema_comparer.model.kinds import ObjectKind
        from cumo_schema_comparer.model.objects import Extension

        key = ObjectKey(ObjectKind.EXTENSION, "public", "plpgsql")
        only_extension = {key: Extension(key=key, version="1.0")}
        result = diff(only_extension, only_extension)
        assert [n.kind for n in result.notes] == [NoteKind.NOTHING_COMPARED]

    def test_one_object_on_either_side_is_enough_to_be_a_real_comparison(self):
        assert [n.kind for n in diff(table("public", "a"), table("public", "a")).notes] == []


class TestColumnAttributes:
    @pytest.mark.parametrize(
        ("master_col", "target_col", "attribute", "severity"),
        [
            (col("c", "int4"), col("c", "int8"), "column.data_type", Severity.ERROR),
            (
                col("c", "text", nullable=True),
                col("c", "text", nullable=False),
                "column.is_nullable",
                Severity.ERROR,
            ),
            (
                col("c", "text", default="'a'"),
                col("c", "text", default="'b'"),
                "column.default",
                Severity.WARNING,
            ),
            (
                col("c", "text", collation="de_DE"),
                col("c", "text", collation="en_US"),
                "column.collation",
                Severity.WARNING,
            ),
        ],
    )
    def test_each_attribute_is_reported_at_its_severity(
        self, master_col, target_col, attribute, severity
    ):
        result = diff(
            table("public", "t", cols=[master_col]),
            table("public", "t", cols=[target_col]),
        )
        finding = findings_by_path(result)["public.t.c"]
        assert finding.status is ObjectStatus.DIFFERS
        assert [d.attribute for d in finding.deltas] == [attribute]
        assert finding.severity is severity

    def test_a_findings_severity_is_the_worst_of_its_deltas(self):
        result = diff(
            table("public", "t", cols=[col("c", "int4", default="'a'")]),
            table("public", "t", cols=[col("c", "int8", default="'b'")]),
        )
        finding = findings_by_path(result)["public.t.c"]
        assert {d.attribute for d in finding.deltas} == {"column.data_type", "column.default"}
        assert finding.severity is Severity.ERROR

    def test_serial_versus_identity_is_reported_rather_than_equated(self):
        # Both autoincrement, but they are genuinely different objects.
        result = diff(
            table(
                "public",
                "t",
                cols=[col("id", "int4", default=SERIAL_SENTINEL, sequence_name="t_id_seq")],
            ),
            table("public", "t", cols=[col("id", "int4", identity="d")]),
        )
        finding = findings_by_path(result)["public.t.id"]
        attributes = {d.attribute for d in finding.deltas}
        assert "column.autoincrement" in attributes

    def test_a_serial_column_whose_sequence_was_renamed_is_in_sync(self):
        # The generated sequence name diverges between environments; the strategy is what counts.
        master = table(
            "public",
            "t",
            cols=[col("id", "int4", default=SERIAL_SENTINEL, sequence_name="t_id_seq")],
        )
        target = table(
            "public",
            "t",
            cols=[col("id", "int4", default=SERIAL_SENTINEL, sequence_name="t_id_seq1")],
        )
        assert diff(master, target).findings == ()

    def test_a_generated_column_is_not_equated_with_a_default(self):
        result = diff(
            table("public", "t", cols=[col("c", "int4", generated="a + b")]),
            table("public", "t", cols=[col("c", "int4", default="a + b")]),
        )
        finding = findings_by_path(result)["public.t.c"]
        assert finding.severity is Severity.ERROR


class TestColumnOrder:
    def _pair(self):
        master = table("public", "t", cols=[col("a"), col("b")])
        target = table("public", "t", cols=[col("b"), col("a")])
        return master, target

    def test_column_order_is_reported_by_default(self):
        master, target = self._pair()
        result = diff(master, target)
        assert {f.key.path for f in result.findings} == {"public.t.a", "public.t.b"}
        assert all(d.attribute == "column.ordinal" for f in result.findings for d in f.deltas)

    def test_column_order_can_be_ignored(self):
        master, target = self._pair()
        result = diff(master, target, options=DiffOptions(ignore_column_order=True))
        assert result.findings == ()


class TestVersionSkew:
    """Different PostgreSQL majors re-print definitions differently.

    Comparing a 15 against a 17 would otherwise report every view, function and check
    constraint as changed.
    """

    def _diff(self):
        master = inventory(
            table("public", "t", cols=[col("c", "text", default="'a'")]),
            label="prod",
            version=150004,
        )
        target = inventory(
            table("public", "t", cols=[col("c", "text", default="'b'")]), label="qa", version=170002
        )
        return diff_inventories(master, target)

    def test_a_note_explains_the_skew(self):
        result = self._diff()
        assert NoteKind.VERSION_SKEW in {n.kind for n in result.notes}

    def test_definition_differences_are_downgraded_to_info(self):
        finding = findings_by_path(self._diff())["public.t.c"]
        assert [d.severity for d in finding.deltas] == [Severity.INFO]

    def test_a_non_definition_difference_keeps_its_severity(self):
        master = inventory(
            table("public", "t", cols=[col("c", "int4")]), label="prod", version=150004
        )
        target = inventory(
            table("public", "t", cols=[col("c", "int8")]), label="qa", version=170002
        )
        finding = findings_by_path(diff_inventories(master, target))["public.t.c"]
        # A type change is a type change regardless of how the server prints things.
        assert finding.severity is Severity.ERROR


class TestGeneratedColumnsAcrossTheTwelveBoundary:
    """An 11 cannot say whether a column is generated, so a comparison with one must not guess.

    ``pg_attribute.attgenerated`` arrived in 12, and the query sends an 11 a literal instead. The
    value that comes back is a fact about the catalog, not about the schema: reported as a
    difference it would be one wrong finding per generated column, and the obvious "fix" would be
    to drop the generated column from the newer side.
    """

    def generated_pair(self, master_version: int, target_version: int):
        master = inventory(
            table("public", "t", cols=[col("c", "numeric", generated="(a * 2)")]),
            label="prod",
            version=master_version,
        )
        target = inventory(
            table("public", "t", cols=[col("c", "numeric", generated=None)]),
            label="qa",
            version=target_version,
        )
        return diff_inventories(master, target)

    def test_an_eleven_against_a_twelve_does_not_compare_generation(self):
        result = self.generated_pair(120000, 110016)
        assert findings_by_path(result).get("public.t.c") is None
        assert NoteKind.GENERATION_UNKNOWN in {n.kind for n in result.notes}

    def test_it_applies_whichever_side_is_the_old_one(self):
        result = self.generated_pair(110016, 150004)
        assert findings_by_path(result).get("public.t.c") is None
        assert NoteKind.GENERATION_UNKNOWN in {n.kind for n in result.notes}

    def test_two_elevens_are_not_warned_about(self):
        """Neither side knows, so neither side disagrees: there is nothing to say."""
        master = inventory(table("public", "t", cols=[col("c", "numeric")]), version=110016)
        target = inventory(
            table("public", "t", cols=[col("c", "numeric")]), label="qa", version=110016
        )
        result = diff_inventories(master, target)
        assert NoteKind.GENERATION_UNKNOWN not in {n.kind for n in result.notes}

    def test_two_servers_that_both_know_still_compare_generation(self):
        """The suppression must not leak into the pairing it was never about."""
        result = self.generated_pair(150004, 150004)
        finding = findings_by_path(result)["public.t.c"]
        assert finding.severity is Severity.ERROR
        assert NoteKind.GENERATION_UNKNOWN not in {n.kind for n in result.notes}

    def test_across_majors_that_both_know_it_is_only_downgraded_not_dropped(self):
        """A 15 against a 17 both know the answer, so the difference is still reported — at INFO,
        because ``generated`` is a printed expression and version skew downgrades those."""
        result = self.generated_pair(150004, 170002)
        assert findings_by_path(result)["public.t.c"].severity is Severity.INFO
        assert NoteKind.GENERATION_UNKNOWN not in {n.kind for n in result.notes}


class TestCollationSkew:
    def test_differing_database_collations_suppress_per_column_collation(self):
        master = inventory(
            table("public", "t", cols=[col("c", "text", collation="de_DE")]),
            label="prod",
            collate="de_DE.utf8",
        )
        target = inventory(
            table("public", "t", cols=[col("c", "text", collation="en_US")]),
            label="qa",
            collate="en_US.utf8",
        )
        result = diff_inventories(master, target)
        # One fact about the databases, not one finding per text column.
        assert result.findings == ()
        assert NoteKind.COLLATION_SKEW in {n.kind for n in result.notes}

    def test_matching_database_collations_still_compare_column_collation(self):
        master = inventory(
            table("public", "t", cols=[col("c", "text", collation="de_DE")]),
            label="prod",
            collate="de_DE.utf8",
        )
        target = inventory(
            table("public", "t", cols=[col("c", "text", collation="en_US")]),
            label="qa",
            collate="de_DE.utf8",
        )
        result = diff_inventories(master, target)
        assert [d.attribute for f in result.findings for d in f.deltas] == ["column.collation"]


class TestSchemaMap:
    def test_a_renamed_schema_is_compared_as_the_same_schema(self):
        master = inventory(
            table("cumo-invoicing", "invoice", cols=[col("id", "int4")]), label="prod"
        )
        target = inventory(table("invoicing_qa", "invoice", cols=[col("id", "int4")]), label="qa")
        result = diff_inventories(master, target, schema_map={"invoicing_qa": "cumo-invoicing"})
        assert result.findings == ()

    def test_without_the_map_everything_looks_missing_and_extra(self):
        master = inventory(
            table("cumo-invoicing", "invoice", cols=[col("id", "int4")]), label="prod"
        )
        target = inventory(table("invoicing_qa", "invoice", cols=[col("id", "int4")]), label="qa")
        result = diff_inventories(master, target)
        assert len(result.findings) == 4


class TestCosmetic:
    def test_a_cosmetic_difference_is_hidden_by_default(self):
        master = table(
            "public",
            "t",
            cols=[col("c", "varchar(50)", raw={"data_type": "character varying(50)"})],
        )
        target = table(
            "public", "t", cols=[col("c", "varchar(50)", raw={"data_type": "varchar(50)"})]
        )
        assert diff(master, target).findings == ()

    def test_show_cosmetic_reveals_what_was_normalized_away(self):
        master = table(
            "public",
            "t",
            cols=[col("c", "varchar(50)", raw={"data_type": "character varying(50)"})],
        )
        target = table(
            "public", "t", cols=[col("c", "varchar(50)", raw={"data_type": "varchar(50)"})]
        )
        result = diff(master, target, options=DiffOptions(show_cosmetic=True))
        delta = findings_by_path(result)["public.t.c"].deltas[0]
        assert delta.cosmetic
        assert delta.severity is Severity.INFO


class TestDeterminism:
    def test_findings_are_ordered_deterministically(self):
        master = shared() | table("public", "b", cols=[col("z"), col("a")]) | table("public", "a")
        result = diff(master, shared())
        paths = [f.key.path for f in result.findings]
        assert paths == sorted(paths, key=lambda p: (p.count("."), p))

    def test_tables_are_reported_before_their_columns(self):
        result = diff(shared() | table("public", "t", cols=[col("c")]), shared())
        kinds = [f.key.kind.value for f in result.findings]
        assert kinds == ["table", "column"]


class TestIgnoreRulesInTheEngine:
    """How suppression interacts with the diff, which is where it can go quietly wrong."""

    def _rules(self, *rules):
        from cumo_schema_comparer.config.model import IgnoreConfig
        from cumo_schema_comparer.diff.ignores import IgnoreRuleSet

        return IgnoreRuleSet(IgnoreConfig.model_validate({"version": 1, "rules": list(rules)}))

    def _diff(self, master_objects, target_objects, rules):
        return diff_inventories(
            inventory(master_objects, label="prod"),
            inventory(target_objects, label="qa"),
            ignores=rules,
        )

    def test_a_suppressed_finding_is_recorded_not_dropped(self):
        # A suppression nobody can see is indistinguishable from a bug in the comparer.
        result = self._diff(
            shared(),
            shared() | table("public", "tmp_debug"),
            self._rules({"id": "scratch", "names": ["public.tmp_*"]}),
        )
        assert result.findings == ()
        assert {f.key.path for f in result.ignored} == {"public.tmp_debug"}
        assert all(f.ignored_by == "scratch" for f in result.ignored)

    def test_a_suppressed_finding_does_not_affect_the_verdict(self):
        result = self._diff(
            shared() | table("public", "tmp_gone"),
            shared(),
            self._rules({"id": "scratch", "names": ["public.tmp_*"]}),
        )
        assert result.worst_severity() is None

    def test_a_clamped_finding_stays_visible_at_the_lower_severity(self):
        result = self._diff(
            shared() | table("public", "invoice"),
            shared(),
            self._rules({"id": "soften", "names": ["public.invoice"], "action": "warn"}),
        )
        finding = findings_by_path(result)["public.invoice"]
        # Was an error; now a warning, and still on the report with the rule that did it.
        assert finding.severity is Severity.WARNING
        assert finding.ignored_by == "soften"

    def test_clamping_never_raises_a_severity(self):
        # A warn rule over an info finding must not promote it.
        result = self._diff(
            shared() | table("public", "t", cols=[col("c", "text")]),
            shared() | table("public", "t", cols=[col("c", "text", collation="C")]),
            self._rules({"id": "soften", "names": ["*"], "action": "warn"}),
        )
        assert all(f.severity <= Severity.WARNING for f in result.findings)

    def test_an_attribute_rule_removes_only_that_difference(self):
        master = shared() | table("public", "t", cols=[col("c", "int4", collation="de_DE")])
        target = shared() | table("public", "t", cols=[col("c", "int8", collation="en_US")])
        result = self._diff(
            master, target, self._rules({"id": "coll", "attributes": ["column.collation"]})
        )
        finding = findings_by_path(result)["public.t.c"]
        assert [d.attribute for d in finding.deltas] == ["column.data_type"]
        assert finding.severity is Severity.ERROR

    def test_an_object_whose_every_difference_is_suppressed_stops_being_a_finding(self):
        master = shared() | table("public", "t", cols=[col("c", "text", collation="de_DE")])
        target = shared() | table("public", "t", cols=[col("c", "text", collation="en_US")])
        result = self._diff(
            master, target, self._rules({"id": "coll", "attributes": ["column.collation"]})
        )
        assert result.findings == ()
        # Nothing to record as ignored either: the object genuinely matches now.
        assert result.ignored == ()

    def test_an_attribute_rule_cannot_excuse_a_missing_object(self):
        result = self._diff(
            shared() | table("public", "gone", cols=[col("c", "text")]),
            shared(),
            self._rules({"id": "coll", "attributes": ["column.collation"]}),
        )
        assert {f.status for f in result.findings} == {ObjectStatus.MISSING_IN_TARGET}

    def test_a_status_scoped_rule_tolerates_extra_but_not_missing(self):
        rules = self._rules(
            {"id": "extra-ok", "names": ["public.scratch*"], "statuses": ["extra_in_target"]}
        )
        tolerated = self._diff(shared(), shared() | table("public", "scratch_a"), rules)
        assert tolerated.findings == ()

        not_tolerated = self._diff(shared() | table("public", "scratch_b"), shared(), rules)
        assert {f.status for f in not_tolerated.findings} == {ObjectStatus.MISSING_IN_TARGET}

    def test_rules_apply_after_rename_reconciliation(self):
        """A rule sees the finding a reader sees.

        Reconciliation turns a missing/extra pair into one "name differs" finding, so a rule scoped
        to ``differs`` has to be able to match it. Applying rules first would let a
        ``statuses: [extra_in_target]`` rule eat half the pair and leave a bogus missing finding.
        """
        from cumo_schema_comparer.model.keys import ObjectKey
        from cumo_schema_comparer.model.kinds import ObjectKind
        from cumo_schema_comparer.model.objects import Index

        master_key = ObjectKey(ObjectKind.INDEX, "public", "t_c_idx")
        target_key = ObjectKey(ObjectKind.INDEX, "public", "idx_t_c")
        master = shared() | {master_key: Index(key=master_key, table="t")}
        target = shared() | {target_key: Index(key=target_key, table="t")}

        result = self._diff(
            master,
            target,
            self._rules({"id": "names", "statuses": ["differs"], "kinds": ["index"]}),
        )
        assert result.findings == ()
        assert [f.ignored_by for f in result.ignored] == ["names"]

    def test_no_rules_means_nothing_is_suppressed(self):
        result = self._diff(shared(), shared() | table("public", "tmp_debug"), None)
        assert result.ignored == ()
        assert result.findings
