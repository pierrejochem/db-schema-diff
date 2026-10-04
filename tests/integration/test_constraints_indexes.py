"""Constraints and indexes against a real PostgreSQL.

Two of these are the phase's whole justification:

* a primary key must be reported **once**, not once as the constraint and again as the index
  PostgreSQL created to back it;
* an index whose name PostgreSQL generated, renamed in one environment, must be **one** warning
  about a name and not a missing index plus an extra one.

Both are the kind of noise that makes a drift report unusable on first contact with a real
environment.
"""

from __future__ import annotations

import pytest

from db_schema_diff.diff.model import ObjectStatus
from db_schema_diff.diff.severity import Severity
from db_schema_diff.model.kinds import ObjectKind
from tests.integration.test_drift_scenarios import attributes_of, summary
from tests.integration.test_no_drift import compare, inventory_of

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def captured(read_only_master):
    """One capture of the baseline schema, for the read-only assertions."""
    return inventory_of(read_only_master, "prod")


def objects_of(inventory, kind: ObjectKind) -> dict[str, object]:
    return {key.path: obj for key, obj in inventory.objects.items() if key.kind is kind}


class TestConstraintBackedIndexDeduplication:
    """A PK or UNIQUE constraint also creates a pg_index row.

    Without the filter, every primary key in the database is reported twice, and a report that
    doubles its own findings is not one anybody trusts.
    """

    def test_primary_keys_are_present_as_constraints(self, captured):
        constraints = objects_of(captured, ObjectKind.CONSTRAINT)
        assert [c for c in constraints.values() if c.contype == "p"]

    def test_no_index_duplicates_a_primary_key(self, captured):
        assert not [n for n in objects_of(captured, ObjectKind.INDEX) if n.endswith("_pkey")]

    def test_no_index_duplicates_a_unique_constraint(self, captured):
        indexes = objects_of(captured, ObjectKind.INDEX)
        assert "acme-invoicing.invoice_number_key" not in indexes
        assert "acme-invoicing.invoice_line_position_key" not in indexes

    def test_the_constraint_records_the_index_that_backs_it(self, captured):
        constraints = objects_of(captured, ObjectKind.CONSTRAINT)
        unique = constraints["acme-invoicing.invoice.invoice_number_key"]
        # Recorded so a report can explain where the index went; never compared, because the name
        # is generated.
        assert unique.backing_index == "invoice_number_key"

    def test_a_standalone_unique_index_is_still_inventoried(self, captured):
        # This one is a real index rather than a constraint's shadow, so it must be present.
        indexes = objects_of(captured, ObjectKind.INDEX)
        assert indexes["acme-invoicing.idx_invoice_number_include"].is_unique


class TestIndexDecomposition:
    def test_a_plain_index_records_its_key_column(self, captured):
        index = objects_of(captured, ObjectKind.INDEX)["acme-invoicing.idx_invoice_mandant"]
        assert index.key_columns == ("mandant_id",)
        assert index.table == "invoice"
        assert index.access_method == "btree"
        assert index.is_unique is False

    def test_an_expression_index_records_the_canonical_expression(self, captured):
        index = objects_of(captured, ObjectKind.INDEX)["acme-invoicing.idx_invoice_number_lower"]
        assert len(index.keys) == 1
        assert index.keys[0].is_expression
        assert "lower" in index.keys[0].expression

    def test_a_partial_index_records_its_predicate(self, captured):
        index = objects_of(captured, ObjectKind.INDEX)["acme-invoicing.idx_invoice_open"]
        assert index.predicate is not None
        assert "OPEN" in index.predicate

    def test_a_descending_key_with_nulls_first_is_decomposed(self, captured):
        index = objects_of(captured, ObjectKind.INDEX)["acme-invoicing.idx_invoice_issued_desc"]
        key = index.keys[0]
        assert key.descending is True
        assert key.nulls_first is True

    def test_include_columns_are_separated_from_key_columns(self, captured):
        index = objects_of(captured, ObjectKind.INDEX)["acme-invoicing.idx_invoice_number_include"]
        assert index.key_columns == ("number",)
        assert index.included_columns == ("mandant_id",)

    def test_a_non_default_opclass_is_recorded(self, captured):
        index = objects_of(captured, ObjectKind.INDEX)["public.idx_mandant_name_pattern"]
        assert index.keys[0].opclass == "varchar_pattern_ops"

    def test_a_default_opclass_is_not_recorded(self, captured):
        # Recording it would put the same value on nearly every index and say nothing.
        index = objects_of(captured, ObjectKind.INDEX)["acme-invoicing.idx_invoice_mandant"]
        assert index.keys[0].opclass is None

    def test_key_order_is_preserved(self, captured):
        index = objects_of(captured, ObjectKind.INDEX)["acme-invoicing.idx_invoice_line_composite"]
        # DESC implies NULLS FIRST in PostgreSQL, and indoption records both bits, so the
        # decomposed key says so explicitly even though the DDL only wrote DESC.
        assert index.key_columns == ("invoice_id", "position DESC NULLS FIRST")

    def test_a_gin_index_records_its_access_method(self, captured):
        index = objects_of(captured, ObjectKind.INDEX)["acme-invoicing.idx_flavours_payload"]
        assert index.access_method == "gin"

    def test_every_index_is_valid_in_a_healthy_schema(self, captured):
        assert all(i.is_valid for i in objects_of(captured, ObjectKind.INDEX).values())


class TestConstraintDecomposition:
    def test_a_foreign_key_records_its_target_and_actions(self, captured):
        constraint = objects_of(captured, ObjectKind.CONSTRAINT)[
            "acme-invoicing.invoice_line.invoice_line_invoice_id_fkey"
        ]
        assert constraint.contype == "f"
        assert constraint.columns == ("invoice_id",)
        assert constraint.references == "acme-invoicing.invoice(id)"
        assert constraint.referential_actions == "ON UPDATE CASCADE ON DELETE CASCADE"

    def test_a_check_constraint_records_a_canonical_expression(self, captured):
        constraint = objects_of(captured, ObjectKind.CONSTRAINT)[
            "acme-invoicing.invoice.invoice_net_amount_check"
        ]
        assert constraint.contype == "c"
        assert constraint.expression is not None
        # The CHECK keyword carries no information and is stripped.
        assert not constraint.expression.upper().startswith("CHECK")

    def test_constraint_columns_are_names_in_order(self, captured):
        constraint = objects_of(captured, ObjectKind.CONSTRAINT)[
            "acme-invoicing.invoice_line.invoice_line_position_key"
        ]
        # Names rather than attribute numbers, which differ between databases whose columns were
        # added in a different order.
        assert constraint.columns == ("invoice_id", "position")

    def test_a_deferrable_constraint_is_recorded_as_such(self, captured):
        constraint = objects_of(captured, ObjectKind.CONSTRAINT)[
            "acme-invoicing.invoice_line.invoice_line_position_key"
        ]
        assert constraint.deferrable is True
        assert constraint.deferred is False

    def test_everything_is_validated_in_a_healthy_schema(self, captured):
        assert all(c.validated for c in objects_of(captured, ObjectKind.CONSTRAINT).values())

    def test_not_null_is_never_reported_as_a_constraint(self, captured):
        """PostgreSQL 17 exposes NOT NULL as a pg_constraint row of type 'n'.

        Including those would invent one constraint per column, so comparing a 15 against a 17
        would produce hundreds of phantom findings.
        """
        types = {c.contype for c in objects_of(captured, ObjectKind.CONSTRAINT).values()}
        assert "n" not in types


class TestRenameReconciliation:
    """The phase's headline case."""

    def test_a_renamed_index_is_one_warning_not_a_missing_and_an_extra(self, databases):
        databases.setup("base", drift="drift_index_renamed")
        result = compare(databases)
        assert summary(result) == {("acme-invoicing.idx_invoice_mandant", "differs", "warning")}
        finding = result.findings[0]
        assert finding.paired_with is not None
        assert finding.paired_with.name == "invoice_mandant_id_idx"
        assert [d.attribute for d in finding.deltas] == ["index.name"]

    def test_a_renamed_index_never_reports_an_error(self, databases):
        # PostgreSQL generated the name, so a difference in it is not a missing index.
        databases.setup("base", drift="drift_index_renamed")
        result = compare(databases)
        assert result.at_or_above(Severity.ERROR) == ()
        # The gate also counts notes and the changelog, which the findings above do not.
        assert (result.worst_severity() or Severity.INFO) < Severity.ERROR


class TestIndexDrift:
    def test_a_missing_index_is_reported(self, databases):
        databases.setup("base", drift="drift_index_missing")
        result = compare(databases)
        assert summary(result) == {
            ("acme-invoicing.idx_invoice_open", "missing_in_target", "error")
        }

    def test_an_extra_index_is_a_warning(self, databases):
        databases.setup("base", drift="drift_extra_index")
        result = compare(databases)
        assert summary(result) == {
            ("acme-invoicing.idx_invoice_note", "extra_in_target", "warning")
        }

    def test_a_changed_predicate_is_an_error(self, databases):
        databases.setup("base", drift="drift_index_predicate")
        result = compare(databases)
        assert summary(result) == {("acme-invoicing.idx_invoice_open", "differs", "error")}
        assert attributes_of(result, "acme-invoicing.idx_invoice_open") == {"index.predicate"}

    def test_losing_uniqueness_is_an_error(self, databases):
        # The target no longer enforces what the master does.
        databases.setup("base", drift="drift_index_uniqueness")
        result = compare(databases)
        assert summary(result) == {
            ("acme-invoicing.idx_invoice_number_include", "differs", "error")
        }
        assert attributes_of(result, "acme-invoicing.idx_invoice_number_include") == {
            "index.is_unique"
        }


class TestConstraintDrift:
    def test_a_changed_on_delete_action_is_an_error(self, databases):
        databases.setup("base", drift="drift_fk_ondelete")
        result = compare(databases)
        assert summary(result) == {
            ("acme-invoicing.invoice_line.invoice_line_invoice_id_fkey", "differs", "error")
        }
        assert attributes_of(
            result, "acme-invoicing.invoice_line.invoice_line_invoice_id_fkey"
        ) == {"constraint.referential_actions"}

    def test_a_changed_check_expression_is_an_error(self, databases):
        databases.setup("base", drift="drift_check_expression")
        result = compare(databases)
        assert summary(result) == {
            ("acme-invoicing.invoice.invoice_net_amount_check", "differs", "error")
        }
        assert attributes_of(result, "acme-invoicing.invoice.invoice_net_amount_check") == {
            "constraint.expression"
        }

    def test_an_unvalidated_constraint_is_an_error(self, databases):
        """NOT VALID means existing rows were never checked.

        Easy to create by accident — add NOT VALID to avoid a long lock, then forget to validate —
        and invisible to anything that only compares constraint names.
        """
        databases.setup("base", drift="drift_constraint_not_valid")
        result = compare(databases)
        assert summary(result) == {
            ("acme-invoicing.invoice.invoice_net_amount_check", "differs", "error")
        }
        assert attributes_of(result, "acme-invoicing.invoice.invoice_net_amount_check") == {
            "constraint.validated"
        }

    def test_a_missing_unique_constraint_is_reported_once(self, databases):
        databases.setup("base", drift="drift_missing_constraint")
        result = compare(databases)
        # Only the constraint: its backing index is excluded from the index inventory, so the
        # finding is not doubled.
        assert summary(result) == {
            ("acme-invoicing.invoice.invoice_number_key", "missing_in_target", "error")
        }

    def test_statuses_are_from_the_masters_point_of_view(self, databases):
        databases.setup("base", drift="drift_missing_constraint")
        assert {f.status for f in compare(databases).findings} == {ObjectStatus.MISSING_IN_TARGET}
