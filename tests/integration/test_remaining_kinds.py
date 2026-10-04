"""Views, sequences, routines, triggers and user-defined types, against a real PostgreSQL.

Several tests here assert that something is *not* reported. Those are the valuable ones: the
catalog generates a great deal of derived material — internal foreign-key triggers, a composite type
per table, a constructor function per range type, a multirange per range — and any of it leaking
into the report would bury whatever is actually wrong.
"""

from __future__ import annotations

import pytest

from db_schema_comparer.diff.severity import Severity
from db_schema_comparer.model.kinds import ObjectKind
from tests.integration.test_drift_scenarios import attributes_of, summary
from tests.integration.test_no_drift import compare, inventory_of

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def captured(read_only_master):
    return inventory_of(read_only_master, "prod")


def of_kind(inventory, kind: ObjectKind) -> dict[str, object]:
    return {key.path: obj for key, obj in inventory.objects.items() if key.kind is kind}


class TestDerivedObjectsAreNotReported:
    """What the catalog generates must not be mistaken for what somebody wrote."""

    def test_only_the_user_triggers_are_inventoried(self, captured):
        # The foreign keys in the fixture create internal trigger pairs. A 200-table schema has
        # thousands of them.
        triggers = of_kind(captured, ObjectKind.TRIGGER)
        assert set(triggers) == {
            "acme-invoicing.invoice.invoice_stamp_issued",
            "acme-invoicing.invoice.invoice_audit",
        }

    def test_a_tables_row_type_is_not_a_composite_type(self, captured):
        # Every table creates a composite type of the same name.
        composites = of_kind(captured, ObjectKind.COMPOSITE_TYPE)
        assert set(composites) == {"acme-invoicing.money_split"}

    def test_range_constructor_functions_are_not_inventoried(self, captured):
        # CREATE TYPE ... AS RANGE generates one per signature; the fixture's single range type
        # produced five before they were filtered out.
        routines = of_kind(captured, ObjectKind.ROUTINE)
        assert not [name for name in routines if "billing_period" in name]

    def test_the_derived_multirange_type_is_not_inventoried(self, captured):
        # PostgreSQL 14+ creates one per range type; it cannot differ without its range differing.
        ranges = of_kind(captured, ObjectKind.RANGE_TYPE)
        assert set(ranges) == {"acme-invoicing.billing_period"}

    def test_an_extensions_own_functions_are_not_inventoried(self, captured):
        # pgcrypto installs a pile of them. The extension is compared as a unit instead.
        routines = of_kind(captured, ObjectKind.ROUTINE)
        assert "public.gen_random_uuid()" not in routines
        assert "public.pgcrypto" in of_kind(captured, ObjectKind.EXTENSION)

    def test_only_authored_routines_remain(self, captured):
        routines = set(of_kind(captured, ObjectKind.ROUTINE))
        assert routines == {
            "acme-invoicing.audit_statement()",
            "acme-invoicing.describe(p_value integer)",
            "acme-invoicing.describe(p_value text)",
            "acme-invoicing.invoice_gross(p_invoice_id integer)",
            "acme-invoicing.mandant_count()",
            "acme-invoicing.next_reference()",
            "acme-invoicing.stamp_issued_at()",
            "acme-invoicing.touch_invoice(p_invoice_id integer)",
        }


class TestViewCapture:
    def test_a_views_columns_and_body_are_captured(self, captured):
        view = of_kind(captured, ObjectKind.VIEW)["acme-invoicing.invoice_summary"]
        # Name and type together, so a view column whose type changed is caught here directly.
        assert view.columns == (
            "id integer",
            "number character varying(40)",
            "mandant_id integer",
            "mandant_name character varying(80)",
            "net_amount numeric(12,2)",
            "gross_amount numeric(14,4)",
            "status character varying(20)",
        )
        assert view.definition
        assert view.is_materialized is False

    def test_a_materialized_view_is_a_distinct_kind(self, captured):
        matviews = of_kind(captured, ObjectKind.MATVIEW)
        assert set(matviews) == {"acme-invoicing.invoice_totals"}
        assert matviews["acme-invoicing.invoice_totals"].is_materialized is True


class TestSequenceCapture:
    def test_the_definition_is_captured(self, captured):
        sequence = of_kind(captured, ObjectKind.SEQUENCE)["acme-invoicing.reference_seq"]
        assert sequence.data_type == "int8"
        assert (sequence.start_value, sequence.increment) == (1000, 10)
        assert (sequence.min_value, sequence.max_value) == (1000, 9999999)
        assert sequence.cache_size == 20
        assert sequence.cycles is True

    def test_a_serials_sequence_records_the_column_that_owns_it(self, captured):
        sequence = of_kind(captured, ObjectKind.SEQUENCE)["acme-invoicing.invoice_id_seq"]
        assert sequence.owned_by == "invoice.id"


class TestRoutineCapture:
    def test_overloads_are_separate_objects(self, captured):
        routines = of_kind(captured, ObjectKind.ROUTINE)
        integer_form = routines["acme-invoicing.describe(p_value integer)"]
        text_form = routines["acme-invoicing.describe(p_value text)"]
        # Without argument-level identity these collapse into one, and each reports the other's body
        # as drift.
        assert integer_form.body_hash != text_form.body_hash

    def test_a_procedure_is_distinguished_from_a_function(self, captured):
        routines = of_kind(captured, ObjectKind.ROUTINE)
        assert routines["acme-invoicing.touch_invoice(p_invoice_id integer)"].prokind == "p"
        assert routines["acme-invoicing.invoice_gross(p_invoice_id integer)"].prokind == "f"

    def test_security_attributes_are_captured(self, captured):
        routine = of_kind(captured, ObjectKind.ROUTINE)["acme-invoicing.mandant_count()"]
        assert routine.security_definer is True
        assert routine.config == ("search_path=public",)

    def test_volatility_and_strictness_are_captured(self, captured):
        routines = of_kind(captured, ObjectKind.ROUTINE)
        assert routines["acme-invoicing.invoice_gross(p_invoice_id integer)"].volatility == "s"
        assert routines["acme-invoicing.describe(p_value integer)"].strict is True

    def test_the_body_is_hashed_not_stored_as_text(self, captured):
        routine = of_kind(captured, ObjectKind.ROUTINE)[
            "acme-invoicing.invoice_gross(p_invoice_id integer)"
        ]
        assert routine.body_hash is not None
        assert len(routine.body_hash) == 64
        # The text is kept for display.
        assert "RAISE EXCEPTION" in (routine.raw.get("body") or "")


class TestTriggerCapture:
    def test_a_row_trigger_with_a_condition_is_decomposed(self, captured):
        trigger = of_kind(captured, ObjectKind.TRIGGER)[
            "acme-invoicing.invoice.invoice_stamp_issued"
        ]
        assert trigger.timing == "BEFORE"
        assert trigger.events == ("INSERT", "UPDATE")
        assert trigger.level == "ROW"
        assert trigger.condition is not None
        assert "OPEN" in trigger.condition
        assert trigger.enabled == "O"

    def test_a_statement_trigger_with_arguments_is_decomposed(self, captured):
        trigger = of_kind(captured, ObjectKind.TRIGGER)["acme-invoicing.invoice.invoice_audit"]
        assert trigger.level == "STATEMENT"
        assert trigger.events == ("INSERT", "UPDATE", "DELETE")
        assert trigger.arguments == ("'invoice'", "'audit'")

    def test_the_condition_comes_from_the_printed_definition(self, captured):
        """pg_get_expr cannot produce it.

        A trigger condition references both NEW and OLD, so pg_get_expr(tgqual, tgrelid) raises
        "expression contains variables of more than one relation". Verified against PostgreSQL 15.
        """
        trigger = of_kind(captured, ObjectKind.TRIGGER)[
            "acme-invoicing.invoice.invoice_stamp_issued"
        ]
        assert trigger.condition is not None


class TestTypeCapture:
    def test_enum_labels_are_captured_in_order(self, captured):
        enum = of_kind(captured, ObjectKind.ENUM_TYPE)["acme-invoicing.dunning_stage"]
        assert enum.labels == ("NONE", "FIRST", "SECOND", "LEGAL")

    def test_a_domain_records_its_base_type_and_constraints(self, captured):
        domain = of_kind(captured, ObjectKind.DOMAIN_TYPE)["acme-invoicing.positive_amount"]
        assert domain.base_type == "numeric(12,2)"
        assert domain.constraints
        # The CHECK keyword is stripped and keywords fold, as for a table constraint.
        assert "value" in domain.constraints[0].lower()

    def test_a_composite_type_records_its_attributes(self, captured):
        composite = of_kind(captured, ObjectKind.COMPOSITE_TYPE)["acme-invoicing.money_split"]
        assert composite.attributes == ("net numeric(12,2)", "tax numeric(12,2)")

    def test_a_range_type_records_its_subtype(self, captured):
        range_type = of_kind(captured, ObjectKind.RANGE_TYPE)["acme-invoicing.billing_period"]
        assert range_type.base_type == "date"


class TestNotDrift:
    """Differences that are not differences. Each would be false drift if mishandled."""

    def test_a_reformatted_function_body_reports_nothing(self, databases):
        # Indentation and blank lines are not behaviour.
        databases.setup("base", drift="drift_function_whitespace")
        assert summary(compare(databases)) == set()

    def test_an_advanced_sequence_reports_nothing(self, databases):
        # A production sequence has always issued more values than a QA one.
        databases.setup("base", drift="drift_sequence_advanced")
        assert summary(compare(databases)) == set()

    def test_trigger_events_in_the_other_order_report_nothing(self, databases):
        # AFTER INSERT OR UPDATE and AFTER UPDATE OR INSERT are the same trigger.
        databases.setup("base", drift="drift_trigger_event_order")
        assert summary(compare(databases)) == set()


class TestViewDrift:
    def test_an_added_view_column_is_an_error(self, databases):
        databases.setup("base", drift="drift_view_column_added")
        result = compare(databases)
        assert summary(result) == {("acme-invoicing.invoice_summary", "differs", "error")}
        assert "view.columns" in attributes_of(result, "acme-invoicing.invoice_summary")

    def test_a_changed_view_body_is_a_warning(self, databases):
        databases.setup("base", drift="drift_view_body")
        result = compare(databases)
        assert summary(result) == {("acme-invoicing.open_invoice", "differs", "warning")}
        assert attributes_of(result, "acme-invoicing.open_invoice") == {"view.definition"}

    def test_a_missing_view_is_an_error(self, databases):
        databases.setup("base", drift="drift_view_missing")
        result = compare(databases)
        assert summary(result) == {
            ("acme-invoicing.invoice_summary", "missing_in_target", "error"),
            ("acme-invoicing.open_invoice", "missing_in_target", "error"),
        }

    def test_a_matview_becoming_a_view_is_reported(self, databases):
        # It no longer holds data and no longer needs refreshing.
        databases.setup("base", drift="drift_matview_to_view")
        result = compare(databases)
        statuses = {(f.key.path, f.status.value) for f in result.findings}
        assert ("acme-invoicing.invoice_totals", "missing_in_target") in statuses
        assert ("acme-invoicing.invoice_totals", "extra_in_target") in statuses


class TestSequenceDrift:
    def test_changed_parameters_are_reported(self, databases):
        databases.setup("base", drift="drift_sequence_params")
        result = compare(databases)
        assert summary(result) == {("acme-invoicing.reference_seq", "differs", "error")}
        assert attributes_of(result, "acme-invoicing.reference_seq") == {
            "sequence.increment",
            "sequence.max_value",
            "sequence.cycles",
        }


class TestRoutineDrift:
    def test_a_changed_body_is_a_warning(self, databases):
        databases.setup("base", drift="drift_function_body")
        result = compare(databases)
        path = "acme-invoicing.invoice_gross(p_invoice_id integer)"
        assert summary(result) == {(path, "differs", "warning")}
        assert attributes_of(result, path) == {"routine.body_hash"}

    def test_losing_security_definer_is_an_error(self, databases):
        databases.setup("base", drift="drift_function_security")
        result = compare(databases)
        path = "acme-invoicing.mandant_count()"
        assert summary(result) == {(path, "differs", "error")}
        # Both are privilege-relevant: who it runs as, and how it resolves names.
        assert attributes_of(result, path) == {"routine.security_definer", "routine.config"}

    def test_a_missing_overload_is_reported_without_touching_the_other(self, databases):
        databases.setup("base", drift="drift_function_overload_missing")
        result = compare(databases)
        assert summary(result) == {
            ("acme-invoicing.describe(p_value text)", "missing_in_target", "error")
        }


class TestTriggerDrift:
    def test_a_disabled_trigger_is_an_error(self, databases):
        databases.setup("base", drift="drift_trigger_disabled")
        result = compare(databases)
        path = "acme-invoicing.invoice.invoice_stamp_issued"
        assert summary(result) == {(path, "differs", "error")}
        assert attributes_of(result, path) == {"trigger.enabled"}

    def test_fewer_events_is_an_error(self, databases):
        databases.setup("base", drift="drift_trigger_events")
        result = compare(databases)
        path = "acme-invoicing.invoice.invoice_stamp_issued"
        assert summary(result) == {(path, "differs", "error")}
        assert attributes_of(result, path) == {"trigger.events"}

    def test_a_changed_condition_is_an_error(self, databases):
        databases.setup("base", drift="drift_trigger_condition")
        result = compare(databases)
        path = "acme-invoicing.invoice.invoice_stamp_issued"
        assert summary(result) == {(path, "differs", "error")}
        assert attributes_of(result, path) == {"trigger.condition"}


class TestTypeDrift:
    def test_a_missing_enum_label_is_an_error(self, databases):
        databases.setup("base", drift="drift_enum_label_missing")
        result = compare(databases)
        assert ("acme-invoicing.dunning_stage", "differs", "error") in summary(result)
        assert "enum_type.labels" in attributes_of(result, "acme-invoicing.dunning_stage")

    def test_a_reordered_enum_is_an_error_not_cosmetic(self, databases):
        """An enum's order defines its comparison operators.

        The same labels in a different order sort differently, so ORDER BY on that column returns a
        different sequence. Real drift.
        """
        databases.setup("base", drift="drift_enum_order")
        result = compare(databases)
        assert ("acme-invoicing.dunning_stage", "differs", "error") in summary(result)

    def test_a_relaxed_domain_constraint_is_an_error(self, databases):
        databases.setup("base", drift="drift_domain_constraint")
        result = compare(databases)
        assert summary(result) == {("acme-invoicing.positive_amount", "differs", "error")}
        assert attributes_of(result, "acme-invoicing.positive_amount") == {
            "domain_type.constraints"
        }

    def test_an_added_composite_attribute_is_an_error(self, databases):
        databases.setup("base", drift="drift_composite_attribute")
        result = compare(databases)
        assert summary(result) == {("acme-invoicing.money_split", "differs", "error")}
        assert attributes_of(result, "acme-invoicing.money_split") == {"composite_type.attributes"}


class TestExtensionDrift:
    def test_a_missing_extension_is_an_error(self, databases):
        databases.setup("base", drift="drift_extension_missing")
        result = compare(databases)
        # Reported once, standing in for every function the extension provides.
        assert ("public.pgcrypto", "missing_in_target", "error") in summary(result)

    def test_its_severity_reaches_the_verdict(self, databases):
        databases.setup("base", drift="drift_extension_missing")
        assert compare(databases).worst_severity() is Severity.ERROR


class TestPartitionedTables:
    """Leaf partitions are counted, not listed.

    Production runs a maintenance job that creates a partition a month; a development database has
    whatever it was seeded with. Listing each leaf would put dozens of missing-table findings in
    every report and bury whatever is actually wrong.
    """

    def test_the_parent_is_inventoried(self, captured):
        tables = of_kind(captured, ObjectKind.TABLE)
        parent = tables["acme-invoicing.invoice_event"]
        assert parent.is_partitioned is True
        assert parent.partition_key is not None
        assert "occurred_at" in parent.partition_key

    def test_the_leaves_are_not_inventoried_individually(self, captured):
        tables = of_kind(captured, ObjectKind.TABLE)
        assert not [name for name in tables if "invoice_event_2026" in name]

    def test_the_parent_records_how_many_leaves_it_has(self, captured):
        parent = of_kind(captured, ObjectKind.TABLE)["acme-invoicing.invoice_event"]
        assert parent.partition_count == 2

    def test_an_unpartitioned_table_has_no_count(self, captured):
        # None rather than zero, so "not partitioned" stays distinct from "no leaves yet".
        assert of_kind(captured, ObjectKind.TABLE)["public.mandant"].partition_count is None

    def test_extra_partitions_are_informational_not_drift(self, databases):
        databases.setup("base", drift="drift_partitions_added")
        result = compare(databases)
        assert summary(result) == {("acme-invoicing.invoice_event", "differs", "info")}
        assert attributes_of(result, "acme-invoicing.invoice_event") == {"table.partition_count"}
        # Visible, but it must not fail a build.
        assert result.at_or_above(Severity.WARNING) == ()
        # The gate also counts notes and the changelog, which the findings above do not.
        assert (result.worst_severity() or Severity.INFO) < Severity.WARNING

    def test_a_changed_partition_key_is_an_error(self, databases):
        """Structural drift: every query relying on partition pruning now scans everything."""
        databases.setup("base", drift="drift_partition_key")
        result = compare(databases)
        finding = next(f for f in result.findings if f.key.path == "acme-invoicing.invoice_event")
        assert "table.partition_key" in {d.attribute for d in finding.deltas}
        assert finding.severity is Severity.ERROR

    def test_a_leafs_columns_are_not_inventoried(self, captured):
        # They are inherited from the parent and cannot differ.
        columns = of_kind(captured, ObjectKind.COLUMN)
        assert not [name for name in columns if "invoice_event_2026" in name]
        assert "acme-invoicing.invoice_event.occurred_at" in columns

    def test_a_partitioned_index_is_inventoried_once(self, captured):
        # PostgreSQL creates a child index on every leaf; only the parent's is a real object.
        indexes = of_kind(captured, ObjectKind.INDEX)
        matching = [name for name in indexes if "invoice_event" in name]
        assert matching == ["acme-invoicing.idx_invoice_event_invoice"]
