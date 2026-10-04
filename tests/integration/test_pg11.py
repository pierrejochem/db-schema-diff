"""PostgreSQL 11, the oldest release this tool inspects.

The floor exists so a server too old to inspect fails with a sentence rather than a SQL error, and
a floor nothing tests is a guess. Two columns the catalog queries want arrived in 12 —
``pg_attribute.attgenerated`` and a table's ``pg_class.relam`` — and the queries never name them on
an older server. This is where that is checked against a real 11 rather than against the gate that
is supposed to do it.

Its own container, like ``test_cross_version.py``, rather than an entry in the CI postgres matrix,
for two reasons: ``base.sql`` declares generated columns that a view and a function in that same
file read, so it cannot be applied to an 11; and the shared harness drops its database with
``WITH (FORCE)``, which arrived in 13. `pg11.sql` is this module's fixture and `apply_sql` is all
it borrows.

Two assertions carry the module. Every kind is captured — a query that names a column the server
lacks fails when it is *planned*, so rows have to come back, not merely a connection. And the same
DDL on two 11s is clean, which is the comparison anyone pointing this at a legacy environment will
actually run.
"""

from __future__ import annotations

import pytest

from db_schema_comparer.build import build_inventory
from db_schema_comparer.config.model import SourceRef
from db_schema_comparer.config.secrets import Dsn
from db_schema_comparer.db.connect import open_connection
from db_schema_comparer.db.features import MINIMUM_VERSION_LABEL, MINIMUM_VERSION_NUM
from db_schema_comparer.diff.engine import diff_inventories
from db_schema_comparer.diff.model import NoteKind
from db_schema_comparer.diff.severity import Severity
from db_schema_comparer.model.kinds import ObjectKind
from tests.integration.conftest import apply_sql, docker_available, dsn_for

pytestmark = pytest.mark.integration

#: The floor itself. Not a newer image: the point is the oldest release that must work.
IMAGE = f"postgres:{MINIMUM_VERSION_LABEL}"

#: Kinds `pg11.sql` creates, and so kinds the capture must find. A kind that silently stopped
#: being captured would otherwise leave this module passing with less and less in it.
EXPECTED_KINDS = {
    ObjectKind.TABLE,
    ObjectKind.COLUMN,
    ObjectKind.CONSTRAINT,
    ObjectKind.INDEX,
    ObjectKind.VIEW,
    ObjectKind.MATVIEW,
    ObjectKind.SEQUENCE,
    ObjectKind.ROUTINE,
    ObjectKind.TRIGGER,
    ObjectKind.ENUM_TYPE,
    ObjectKind.DOMAIN_TYPE,
    ObjectKind.COMPOSITE_TYPE,
    ObjectKind.RANGE_TYPE,
    ObjectKind.EXTENSION,
}


@pytest.fixture(scope="module")
def two_elevens():
    """Two PostgreSQL 11 databases built from the same file."""
    if not docker_available():
        pytest.skip("needs Docker")

    postgres = pytest.importorskip("testcontainers.community.postgres")

    containers = []
    try:
        dsns = []
        for _ in range(2):
            container = postgres.PostgresContainer(IMAGE, driver=None)
            container.start()
            containers.append(container)
            dsn = dsn_for(container.get_connection_url(), container.dbname)
            apply_sql(dsn, "pg11")
            dsns.append(dsn)
        yield tuple(dsns)
    finally:
        for container in reversed(containers):
            container.stop()


def capture(dsn: str, label: str):
    source = SourceRef(label=label, dsn_env=f"{label.upper()}_DSN")
    with open_connection(Dsn(dsn, env_name=source.dsn_env), label=label) as connection:
        return build_inventory(connection, source)


@pytest.fixture(scope="module")
def inventories(two_elevens):
    master, target = two_elevens
    return capture(master, "prod"), capture(target, "qa")


class TestTheServerIsReallyEleven:
    def test_it_is_the_floor_and_not_something_newer(self, inventories):
        master, _ = inventories
        assert master.source.major_version == 11
        assert master.source.server_version_num >= MINIMUM_VERSION_NUM
        assert master.source.server_version_num < 120000

    def test_an_eleven_is_accepted_rather_than_refused(self, inventories):
        """The connection was opened by the fixture, so reaching here is the assertion.

        ``_require_supported_version`` runs inside ``open_connection``; a floor still at 12 would
        have failed the fixture, not this test.
        """
        master, _ = inventories
        assert master.source.server_version.startswith("11.")


class TestEveryKindIsCaptured:
    """A query naming a column the server lacks fails when it is planned, so this is the test that
    proves the gates work — an empty database would prove only that the SQL parses."""

    def test_every_kind_the_fixture_creates_comes_back(self, inventories):
        master, _ = inventories
        assert set(master.counts()) == EXPECTED_KINDS

    def test_an_include_index_keeps_its_payload_out_of_the_key(self, inventories):
        """``pg_index.indnkeyatts`` arrived in 11 and is named unconditionally. Read without it,
        this three-column index would look like a three-column key, and an INCLUDE column would
        read as part of what the index enforces."""
        master, _ = inventories
        indexes = {key.name: value for key, value in master.by_kind(ObjectKind.INDEX).items()}
        receipt = indexes["receipt_invoice_idx"]
        keys = [(k.expression, k.included) for k in receipt.keys]
        assert keys == [("invoice_id", False), ("paid_at", False), ("amount", True)]

    def test_a_procedure_is_told_apart_from_a_function(self, inventories):
        """``pg_proc.prokind`` arrived in 11; before it, two booleans said the same thing."""
        master, _ = inventories
        routines = master.by_kind(ObjectKind.ROUTINE)
        kinds = {value.prokind for value in routines.values()}
        assert "p" in kinds, "the procedure was captured as a function"
        assert "f" in kinds

    def test_a_hash_partitioned_table_is_captured(self, inventories):
        """Hash partitioning arrived in 11."""
        master, _ = inventories
        tables = {key.name: value for key, value in master.by_kind(ObjectKind.TABLE).items()}
        assert tables["ledger"].is_partitioned
        assert "HASH" in tables["ledger"].partition_key.upper()

    def test_no_column_is_reported_as_generated(self, inventories):
        """An 11 is sent a literal in place of ``attgenerated``, and the literal means "not
        generated". If this ever returned a value, the gate has stopped firing."""
        master, _ = inventories
        columns = master.by_kind(ObjectKind.COLUMN)
        assert columns, "no columns captured"
        assert all(value.generated is None for value in columns.values())


class TestTwoElevensAreClean:
    """The comparison someone pointing this at a legacy environment will actually run.

    Both databases were built from one file, so anything reported at WARNING or above is a bug in
    this tool rather than a fact about the schemas.
    """

    def test_nothing_is_reported_above_info(self, inventories):
        result = diff_inventories(*inventories)
        loud = [
            f"{f.key.path}: {d.attribute}"
            for f in result.findings
            for d in f.deltas
            if d.severity >= Severity.WARNING
        ]
        assert loud == []

    def test_no_object_is_missing_or_extra(self, inventories):
        result = diff_inventories(*inventories)
        assert [f.key.path for f in result.findings if not f.deltas] == []

    def test_neither_version_nor_generation_is_warned_about(self, inventories):
        """Same major and neither side knows about generated columns, so neither note applies:
        there is no skew and nothing was suppressed."""
        result = diff_inventories(*inventories)
        kinds = {n.kind for n in result.notes}
        assert NoteKind.VERSION_SKEW not in kinds
        assert NoteKind.GENERATION_UNKNOWN not in kinds
