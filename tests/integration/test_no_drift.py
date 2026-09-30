"""The single most valuable test in the suite.

Apply the same DDL to two databases and assert **zero** findings at WARNING or above. Almost
every normalisation bug shows up here first, because the catalog offers so many ways to make two
identical schemas look different: generated sequence names, attnum holes, long type spellings,
re-printed defaults.

If this test is green, the tool can be trusted on real environments. If it is red, nothing else
matters.
"""

from __future__ import annotations

import pytest

from cumo_schema_comparer.build import build_inventory
from cumo_schema_comparer.config.model import SourceRef
from cumo_schema_comparer.config.secrets import Dsn
from cumo_schema_comparer.db.connect import open_connection
from cumo_schema_comparer.diff.engine import diff_inventories
from cumo_schema_comparer.diff.severity import Severity
from cumo_schema_comparer.model.kinds import ObjectKind

pytestmark = pytest.mark.integration


def inventory_of(dsn_text: str, label: str):
    source = SourceRef(label=label, dsn_env=f"{label.upper()}_DSN")
    dsn = Dsn(dsn_text, env_name=source.dsn_env)
    with open_connection(dsn, label=label) as connection:
        return build_inventory(connection, source)


def compare(databases, **kwargs):
    return diff_inventories(
        inventory_of(databases.master_dsn, "prod"),
        inventory_of(databases.target_dsn, "qa"),
        **kwargs,
    )


class TestIdenticalSchemas:
    def test_identical_databases_report_no_drift(self, databases):
        databases.setup("base")
        result = compare(databases)
        significant = [f for f in result.findings if f.severity >= Severity.WARNING]
        assert significant == [], _explain(significant)

    def test_identical_databases_report_nothing_at_all(self, databases):
        # Stricter: not even an INFO. Any finding here is a normalisation gap.
        databases.setup("base")
        result = compare(databases)
        assert result.findings == (), _explain(list(result.findings))

    def test_the_fingerprints_match(self, databases):
        databases.setup("base")
        master = inventory_of(databases.master_dsn, "prod")
        target = inventory_of(databases.target_dsn, "qa")
        assert master.fingerprint() == target.fingerprint()


class TestCatalogTraps:
    """Each of these would produce false drift if one specific detail were handled naively."""

    def test_a_serial_columns_generated_sequence_name_is_not_compared(self, databases):
        databases.setup("base")
        master = inventory_of(databases.master_dsn, "prod")
        column = next(
            obj for key, obj in master.objects.items() if key.path == "cumo-invoicing.invoice.id"
        )
        # The default canonicalises to a sentinel; the real name is recorded for display only.
        assert column.is_serial
        assert column.sequence_name is not None

    def test_a_dropped_column_does_not_shift_later_ordinals(self, databases):
        # audit_entry drops its second column, leaving a permanent hole in attnum.
        databases.setup("base")
        master = inventory_of(databases.master_dsn, "prod")
        # Filtered by kind: a constraint's key also carries its table in `name` and its own
        # name in `subname`, so a kind-blind filter would pick those up too.
        ordinals = {
            key.subname: obj.ordinal
            for key, obj in master.objects.items()
            if key.kind is ObjectKind.COLUMN and key.name == "audit_entry"
        }
        assert ordinals == {"id": 1, "actor": 2, "happened_at": 3, "payload": 4}

    def test_long_type_spellings_are_canonicalized(self, databases):
        databases.setup("base")
        master = inventory_of(databases.master_dsn, "prod")
        by_path = {key.path: obj for key, obj in master.objects.items()}
        assert by_path["public.mandant.name"].data_type == "varchar(80)"
        assert by_path["public.mandant.created_at"].data_type == "timestamp"
        assert by_path["cumo-invoicing.invoice.issued_at"].data_type == "timestamptz"
        # The server's own spelling is kept for display.
        assert by_path["public.mandant.name"].raw.get("data_type") == "character varying(80)"

    def test_the_hyphenated_schema_and_quoted_names_survive(self, databases):
        databases.setup("base")
        master = inventory_of(databases.master_dsn, "prod")
        paths = {key.path for key in master.objects}
        assert "cumo-invoicing.DunningLevel" in paths
        assert "cumo-invoicing.DunningLevel.Name" in paths
        assert "public.QRTZ_LOCKS" in paths

    def test_a_generated_column_is_captured_as_generated_not_as_a_default(self, databases):
        databases.setup("base")
        master = inventory_of(databases.master_dsn, "prod")
        column = next(
            obj
            for key, obj in master.objects.items()
            if key.path == "cumo-invoicing.invoice.gross_amount"
        )
        assert column.generated is not None
        assert column.default is None

    def test_an_identity_column_is_distinguished_from_a_serial(self, databases):
        databases.setup("base")
        master = inventory_of(databases.master_dsn, "prod")
        by_path = {key.path: obj for key, obj in master.objects.items()}
        assert by_path["cumo-invoicing.invoice_line.id"].autoincrement == "identity:d"
        assert by_path["cumo-invoicing.invoice.id"].autoincrement == "serial"


def _explain(findings) -> str:
    if not findings:
        return ""
    lines = ["unexpected findings on identical schemas:"]
    for finding in findings:
        lines.append(f"  {finding.severity.label} {finding.status.value} {finding.key.path}")
        for delta in finding.deltas:
            lines.append(
                f"      {delta.attribute}: {delta.master_value!r} vs {delta.target_value!r}"
            )
    return "\n".join(lines)


class TestOrdinalsAreIndependentOfIndexes:
    """A column's ordinal must depend only on the columns.

    This is a regression test for a real bug in this tool's own catalog query. An index depends on
    its table's columns through ``pg_depend`` with ``deptype = 'a'`` — the same dependency kind a
    serial column's owned sequence uses. Joining ``pg_depend`` directly and filtering for sequences
    afterwards therefore emitted one row per dependent index, which inflated the dense ordinal by a
    count that varied with how many indexes mentioned the column.

    The no-drift test could not catch it, because both databases had the same indexes.
    """

    EXPECTED = (
        "id",
        "mandant_id",
        "number",
        "net_amount",
        "tax_rate",
        "gross_amount",
        "note",
        "issued_at",
        "status",
    )

    def _ordinals(self, dsn):
        inventory = inventory_of(dsn, "prod")
        return {
            key.subname: obj.ordinal
            for key, obj in inventory.objects.items()
            if key.kind is ObjectKind.COLUMN and key.qualified == "cumo-invoicing.invoice"
        }

    def test_ordinals_are_dense_and_start_at_one(self, databases):
        databases.setup("base")
        ordinals = self._ordinals(databases.master_dsn)
        assert ordinals == {name: n for n, name in enumerate(self.EXPECTED, start=1)}

    def test_dropping_an_index_changes_no_ordinal(self, databases):
        databases.setup("base", drift="drift_index_missing")
        assert self._ordinals(databases.master_dsn) == self._ordinals(databases.target_dsn)

    def test_adding_an_index_changes_no_ordinal(self, databases):
        databases.setup("base", drift="drift_extra_index")
        assert self._ordinals(databases.master_dsn) == self._ordinals(databases.target_dsn)

    def test_a_serial_column_still_finds_its_owned_sequence(self, databases):
        # The narrowed join must not lose the sequence it was there to find.
        databases.setup("base")
        inventory = inventory_of(databases.master_dsn, "prod")
        column = next(
            obj for key, obj in inventory.objects.items() if key.path == "cumo-invoicing.invoice.id"
        )
        assert column.is_serial
        assert column.sequence_name == "invoice_id_seq"
