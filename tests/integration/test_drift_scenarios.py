"""One deliberate difference per test, against a real PostgreSQL.

Each test asserts the *exact* finding set, and that nothing else was reported. Asserting only
that the expected finding is present would let a leaky normaliser hide behind a passing test: the
extra noise it produces would go unnoticed until someone ran the tool on a real database.
"""

from __future__ import annotations

import pytest

from db_schema_comparer.diff.model import ObjectStatus
from db_schema_comparer.diff.severity import Severity
from tests.integration.test_no_drift import compare

pytestmark = pytest.mark.integration


def summary(result) -> set[tuple[str, str, str]]:
    """Every finding as ``(path, status, severity)``, for exact comparison."""
    return {(f.key.path, f.status.value, f.severity.label) for f in result.findings}


def attributes_of(result, path: str) -> set[str]:
    finding = next(f for f in result.findings if f.key.path == path)
    return {d.attribute for d in finding.deltas}


class TestColumnType:
    def test_a_widened_column_is_one_error_and_nothing_else(self, databases):
        databases.setup("base", drift="drift_column_type")
        result = compare(databases)
        assert summary(result) == {("acme-invoicing.invoice_line.position", "differs", "error")}
        assert attributes_of(result, "acme-invoicing.invoice_line.position") == {"column.data_type"}


class TestMissingTable:
    def test_a_missing_table_reports_the_table_its_columns_and_its_constraints(self, databases):
        databases.setup("base", drift="drift_missing_table")
        result = compare(databases)
        # Dropping the table takes its primary key with it. Reporting each object is right: a
        # reader should not have to infer that a missing table implies a missing key.
        assert summary(result) == {
            ("acme-invoicing.DunningLevel", "missing_in_target", "error"),
            ("acme-invoicing.DunningLevel.id", "missing_in_target", "error"),
            ("acme-invoicing.DunningLevel.Name", "missing_in_target", "error"),
            ("acme-invoicing.DunningLevel.DunningLevel_pkey", "missing_in_target", "error"),
        }


class TestNullability:
    def test_a_dropped_not_null_is_an_error(self, databases):
        databases.setup("base", drift="drift_nullable")
        result = compare(databases)
        assert summary(result) == {("acme-invoicing.invoice.number", "differs", "error")}
        assert attributes_of(result, "acme-invoicing.invoice.number") == {"column.is_nullable"}


class TestDefault:
    def test_a_changed_default_is_a_warning_not_an_error(self, databases):
        # A different default does not break existing rows or queries.
        databases.setup("base", drift="drift_default_changed")
        result = compare(databases)
        assert summary(result) == {("acme-invoicing.invoice.status", "differs", "warning")}
        assert attributes_of(result, "acme-invoicing.invoice.status") == {"column.default"}


class TestExtraObjects:
    def test_an_extra_table_in_the_target_never_reports_an_error(self, databases):
        databases.setup("base", drift="drift_extra_table")
        result = compare(databases)
        assert summary(result) == {
            ("public.tmp_debug", "extra_in_target", "warning"),
            ("public.tmp_debug.id", "extra_in_target", "warning"),
            ("public.tmp_debug.note", "extra_in_target", "warning"),
        }
        assert result.worst_severity() is Severity.WARNING


class TestDroppedColumnOrdinal:
    def test_the_same_final_shape_reached_by_different_routes_is_in_sync(self, databases):
        """The master dropped a column; the target never had it.

        The live columns are identical, but the master's attnum values have a hole and the
        target's do not. Comparing attnum would report every column after the hole as drift. This
        is the single easiest bug to ship, and this test is the only thing that catches it.
        """
        databases.setup("base", drift="drift_dropped_column_ordinal")
        result = compare(databases)
        assert summary(result) == set()


class TestSeverityGate:
    def test_only_errors_pass_an_error_gate(self, databases):
        databases.setup("base", drift="drift_extra_table")
        result = compare(databases)
        assert result.at_or_above(Severity.ERROR) == ()
        assert (result.worst_severity() or Severity.INFO) < Severity.ERROR
        assert len(result.at_or_above(Severity.WARNING)) == 3

    def test_an_error_is_reported_above_both_gates(self, databases):
        databases.setup("base", drift="drift_column_type")
        result = compare(databases)
        assert len(result.at_or_above(Severity.ERROR)) == 1
        assert len(result.at_or_above(Severity.WARNING)) == 1


class TestMultipleDrifts:
    def test_independent_differences_are_reported_independently(self, databases):
        databases.setup("base")
        from tests.integration.conftest import apply_sql

        apply_sql(databases.target_dsn, "drift_nullable")
        apply_sql(databases.target_dsn, "drift_extra_table")
        result = compare(databases)
        assert summary(result) == {
            ("acme-invoicing.invoice.number", "differs", "error"),
            ("public.tmp_debug", "extra_in_target", "warning"),
            ("public.tmp_debug.id", "extra_in_target", "warning"),
            ("public.tmp_debug.note", "extra_in_target", "warning"),
        }

    def test_an_empty_target_does_not_enumerate_every_object(self, databases):
        from db_schema_comparer.diff.model import NoteKind
        from tests.integration.conftest import apply_sql, reset

        apply_sql(databases.master_dsn, "base")
        reset(databases.target_dsn)
        result = compare(databases)
        assert result.findings == ()
        assert [n.kind for n in result.notes] == [NoteKind.TARGET_EMPTY]


class TestStatusDirection:
    def test_missing_and_extra_are_reported_from_the_masters_point_of_view(self, databases):
        databases.setup("base", drift="drift_missing_table")
        result = compare(databases)
        statuses = {f.status for f in result.findings}
        # The master has it and the target does not, so it is missing *in the target*.
        assert statuses == {ObjectStatus.MISSING_IN_TARGET}


class TestCosmeticOnly:
    """Two environments that spelled the same defaults differently are in sync.

    Verified against PostgreSQL 15: ``pg_attrdef`` keeps ``now()`` and ``CURRENT_TIMESTAMP`` as
    written, so this difference is real in the catalog and must be normalised away here.
    """

    def test_differently_spelled_defaults_report_no_drift(self, databases):
        databases.setup("base", drift="drift_cosmetic_only")
        result = compare(databases)
        assert summary(result) == set()

    def test_the_raw_catalog_values_really_do_differ(self, databases):
        # Without this, the test above would also pass if the server had canonicalised the
        # defaults itself, and would prove nothing about the normaliser.
        databases.setup("base", drift="drift_cosmetic_only")
        from tests.integration.test_no_drift import inventory_of

        master = {k.path: v for k, v in inventory_of(databases.master_dsn, "prod").objects.items()}
        target = {k.path: v for k, v in inventory_of(databases.target_dsn, "qa").objects.items()}
        differing = [
            path
            for path, obj in master.items()
            if getattr(obj, "raw", None)
            and obj.raw.get("default") != target[path].raw.get("default")
        ]
        assert differing, "the fixture no longer produces a raw catalog difference"

    def test_show_cosmetic_makes_the_normalization_visible(self, databases):
        databases.setup("base", drift="drift_cosmetic_only")
        from db_schema_comparer.diff.engine import diff_inventories
        from db_schema_comparer.diff.model import DiffOptions
        from tests.integration.test_no_drift import inventory_of

        result = diff_inventories(
            inventory_of(databases.master_dsn, "prod"),
            inventory_of(databases.target_dsn, "qa"),
            options=DiffOptions(show_cosmetic=True),
        )
        assert result.findings
        assert all(d.cosmetic for f in result.findings for d in f.deltas)
