"""The same schema on two different PostgreSQL majors.

The scenario the version-skew rule exists for, and the one most likely to make this tool useless
if mishandled: production on 13 and a developer's container on 17 are identical schemas that the
catalog describes differently. Every definition the server re-prints — view bodies, check
expressions, index expressions, routine headers — can come back with different formatting.

Two containers rather than two databases, because the whole point is two server versions.
"""

from __future__ import annotations

import pytest

from cumo_schema_comparer.build import build_inventory
from cumo_schema_comparer.config.model import SourceRef
from cumo_schema_comparer.config.secrets import Dsn
from cumo_schema_comparer.db.connect import open_connection
from cumo_schema_comparer.diff.engine import diff_inventories
from cumo_schema_comparer.diff.model import NoteKind
from cumo_schema_comparer.diff.severity import Severity
from tests.integration.conftest import apply_sql, docker_available, dsn_for

pytestmark = pytest.mark.integration

OLDER = "postgres:13"
NEWER = "postgres:17"


@pytest.fixture(scope="module")
def two_versions():
    """One database on an older major and one on a newer, both with the same DDL."""
    if not docker_available():
        pytest.skip("needs Docker")

    postgres = pytest.importorskip("testcontainers.community.postgres")

    containers = []
    try:
        for image in (OLDER, NEWER):
            container = postgres.PostgresContainer(image, driver=None)
            container.start()
            containers.append(container)

        dsns = []
        for container in containers:
            base = container.get_connection_url()
            dsn = dsn_for(base, container.dbname)
            apply_sql(dsn, "base")
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
def inventories(two_versions):
    older, newer = two_versions
    return capture(older, "prod"), capture(newer, "qa")


class TestVersionsDiffer:
    def test_the_two_servers_really_are_different_majors(self, inventories):
        master, target = inventories
        assert master.source.major_version != target.source.major_version
        assert (master.source.major_version, target.source.major_version) == (13, 17)


class TestSkewIsReported:
    def test_a_note_explains_the_skew(self, inventories):
        result = diff_inventories(*inventories)
        assert NoteKind.VERSION_SKEW in {n.kind for n in result.notes}

    def test_definition_differences_are_downgraded_to_info(self, inventories):
        """The server re-prints definitions, and that printing changes between majors.

        Without the downgrade, every view and check constraint in the database reads as changed and
        the report is unusable.
        """
        result = diff_inventories(*inventories)
        body_attributes = {
            "view.definition",
            "constraint.expression",
            "matview.definition",
            "routine.body_hash",
        }
        for finding in result.findings:
            for delta in finding.deltas:
                if delta.attribute in body_attributes:
                    assert delta.severity is Severity.INFO, (
                        f"{finding.key.path} {delta.attribute} was not downgraded"
                    )


class TestNoStructuralDrift:
    """The interesting assertion: identical DDL on two majors must not read as *structural* drift.

    Anything reported at error level here is a real bug in this tool, because the two databases were
    built from the same file.
    """

    def test_no_object_is_missing_or_extra(self, inventories):
        result = diff_inventories(*inventories)
        wrong = [
            f for f in result.findings if f.status.value in ("missing_in_target", "extra_in_target")
        ]
        assert wrong == [], _explain(wrong)

    def test_nothing_is_reported_at_error_level(self, inventories):
        result = diff_inventories(*inventories)
        errors = [f for f in result.findings if f.severity is Severity.ERROR]
        assert errors == [], _explain(errors)

    def test_routine_identities_match_across_versions(self, inventories):
        """A procedure's argument list is spelled differently on 13 and 15+.

        It is part of the routine's key, so without canonicalising it every procedure would show up
        as both missing and extra here.
        """
        from cumo_schema_comparer.model.kinds import ObjectKind

        master, target = inventories

        def names(inventory):
            return {key.name for key in inventory.objects if key.kind is ObjectKind.ROUTINE}

        assert names(master) == names(target)

    def test_column_types_match_across_versions(self, inventories):
        from cumo_schema_comparer.model.kinds import ObjectKind

        master, target = inventories

        def types(inv):
            return {
                key.path: obj.data_type
                for key, obj in inv.objects.items()
                if key.kind is ObjectKind.COLUMN
            }

        assert types(master) == types(target)

    def test_not_null_never_appears_as_a_constraint(self, inventories):
        """PostgreSQL 17 exposes NOT NULL as a pg_constraint row of type 'n'.

        Reading it there would invent one constraint per column on the 17 side alone, so a
        13-against-17 comparison would report hundreds of phantom extra constraints.
        """
        from cumo_schema_comparer.model.kinds import ObjectKind

        for inventory in inventories:
            types = {
                obj.contype
                for key, obj in inventory.objects.items()
                if key.kind is ObjectKind.CONSTRAINT
            }
            assert "n" not in types


def _explain(findings) -> str:
    if not findings:
        return ""
    lines = ["identical DDL on two majors produced:"]
    for finding in findings:
        lines.append(f"  {finding.severity.label} {finding.status.value} {finding.key.path}")
        for delta in finding.deltas:
            lines.append(
                f"      {delta.attribute}: {delta.master_value!r} vs {delta.target_value!r}"
            )
    return "\n".join(lines)
