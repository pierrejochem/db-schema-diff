"""Liquibase changelog comparison against a real PostgreSQL.

The fixture's changelog deliberately lives in the quoted, hyphenated ``"cumo-invoicing"`` schema,
mirroring what ``spring.liquibase.liquibase-schema=cumo-invoicing`` actually produces. So every test
here also exercises locating the table rather than assuming ``public``, and quoting a hyphenated
identifier on the way in.
"""

from __future__ import annotations

import pytest

from db_schema_comparer.diff.changelog import ChangelogOptions, ChangelogStatus
from db_schema_comparer.diff.severity import Severity
from tests.integration.test_no_drift import compare, inventory_of

pytestmark = pytest.mark.integration


def changelog_of(databases, drift: str | None = None, **kwargs):
    databases.setup("base", drift=drift)
    return compare(databases, **kwargs).changelog


def _gate(target, fail_on: str):
    """The report the exit code is computed from, built around one target."""
    from db_schema_comparer.diff.model import ComparisonReport

    return ComparisonReport(name="t", master_label="prod", targets=(target,), fail_on=fail_on)


class TestLocating:
    """The table is found, not assumed."""

    def test_the_hyphenated_schema_is_located_without_configuration(self, databases):
        databases.setup("base")
        inventory = inventory_of(databases.master_dsn, "prod")
        assert inventory.changelog is not None
        assert inventory.changelog.location is not None
        # Verified against the real cumo-invoicing setup: not public.
        assert inventory.changelog.location.schema == "cumo-invoicing"
        assert inventory.changelog.location.table == "DATABASECHANGELOG"

    def test_the_rows_are_read_in_deployment_order(self, databases):
        databases.setup("base")
        inventory = inventory_of(databases.master_dsn, "prod")
        rows = inventory.changelog.rows
        assert [r.id for r in rows] == ["initial-schema", "add-invoice-line", "add-dunning"]
        assert [r.order_executed for r in rows] == [1, 2, 3]

    def test_the_column_list_is_introspected(self, databases):
        databases.setup("base")
        inventory = inventory_of(databases.master_dsn, "prod")
        # Selecting a column that does not exist would fail the whole query, so the real list is
        # read first and intersected with what this build knows about.
        assert {"ID", "AUTHOR", "MD5SUM", "DEPLOYMENT_ID"} <= inventory.changelog.columns_present

    def test_the_lock_state_is_read(self, databases):
        databases.setup("base")
        inventory = inventory_of(databases.master_dsn, "prod")
        assert inventory.changelog.lock_held is False

    def test_the_tag_is_read(self, databases):
        databases.setup("base")
        inventory = inventory_of(databases.master_dsn, "prod")
        assert inventory.changelog.last_tag == "R7.6.2"

    def test_skipping_liquibase_leaves_no_changelog(self, databases):
        from db_schema_comparer.build import build_inventory
        from db_schema_comparer.config.model import SourceRef
        from db_schema_comparer.config.secrets import Dsn
        from db_schema_comparer.db.connect import open_connection

        databases.setup("base")
        source = SourceRef(label="prod", dsn_env="X")
        with open_connection(Dsn(databases.master_dsn, env_name="X"), label="prod") as connection:
            inventory = build_inventory(connection, source, skip_liquibase=True)
        assert inventory.changelog is None


class TestInSync:
    def test_identical_changelogs_are_in_sync(self, databases):
        result = changelog_of(databases)
        assert result.status is ChangelogStatus.IN_SYNC
        assert result.behind_count == 0
        assert result.master_count == result.target_count == 3

    def test_an_in_sync_changelog_does_not_affect_the_verdict(self, databases):
        databases.setup("base")
        assert compare(databases).worst_severity() is None


class TestBehind:
    def test_a_target_behind_is_an_error_naming_the_first_missing_changeset(self, databases):
        result = changelog_of(databases, "drift_changelog_behind")
        assert result.status is ChangelogStatus.TARGET_BEHIND
        assert result.severity is Severity.ERROR
        assert result.behind_count == 2
        assert result.first_divergence == ("add-invoice-line", "kolowae")

    def test_the_headline_is_actionable(self, databases):
        result = changelog_of(databases, "drift_changelog_behind")
        headline = result.headline("prod", "qa")
        assert "qa is 2 changeset(s) behind prod" in headline
        assert "add-invoice-line" in headline

    def test_the_tag_line_shows_both_releases(self, databases):
        result = changelog_of(databases, "drift_changelog_behind")
        # prod is tagged R7.6.2; qa lost the tagged changeset with the others.
        assert result.master_tag == "R7.6.2"
        assert result.target_tag is None

    def test_it_drives_the_targets_worst_severity(self, databases):
        databases.setup("base", drift="drift_changelog_behind")
        # The DDL is identical, so the changelog is the only thing that can fail this.
        result = compare(databases)
        assert result.findings == ()
        assert result.worst_severity() is Severity.ERROR


class TestChecksums:
    def test_an_edited_changeset_is_an_error(self, databases):
        result = changelog_of(databases, "drift_changelog_checksum")
        assert result.status is ChangelogStatus.CHECKSUM_MISMATCH
        assert result.severity is Severity.ERROR
        assert [m.ref for m in result.checksum_mismatches] == [("add-invoice-line", "kolowae")]

    def test_an_algorithm_upgrade_is_one_note_not_three_errors(self, databases):
        """The fixture rewrites every checksum with an 8: prefix.

        A comparer that string-compares checksums reports all three changesets as edited. The real
        answer is that the algorithms differ and individual checksums are not comparable.
        """
        result = changelog_of(databases, "drift_changelog_algorithm")
        assert result.checksum_algorithm_skew is True
        assert result.checksum_mismatches == ()
        assert result.status is ChangelogStatus.HISTORY_DIFFERS
        assert result.severity is Severity.WARNING

    def test_the_algorithm_note_explains_itself(self, databases):
        result = changelog_of(databases, "drift_changelog_algorithm")
        assert any("algorithm versions" in n for n in result.notes)


class TestExecType:
    def test_mark_ran_is_a_warning_not_a_failure(self, databases):
        result = changelog_of(databases, "drift_changelog_markran")
        assert result.status is ChangelogStatus.HISTORY_DIFFERS
        assert result.severity is Severity.WARNING
        assert [d.ref for d in result.exectype_differences] == [("add-dunning", "kolowae")]

    def test_mark_ran_does_not_fail_an_error_gate(self, databases):
        databases.setup("base", drift="drift_changelog_markran")
        result = compare(databases)
        # The gate itself, not the findings: the changelog verdict is not a finding.
        assert not _gate(result, "error").has_drift(Severity.ERROR)

    def test_a_failed_changeset_is_an_error(self, databases):
        result = changelog_of(databases, "drift_changelog_failed")
        assert result.severity is Severity.ERROR
        assert _gate(compare(databases), "error").has_drift(Severity.ERROR)
        assert ("add-dunning", "kolowae") in [f.ref for f in result.failed_changesets]


class TestFilename:
    def test_a_different_path_does_not_read_as_a_missing_changeset(self, databases):
        """The case that makes FILENAME unusable as identity.

        cumo-invoicing's master.xml mixes relative and non-relative includes, so the same changeset
        records a different filename in different deployments.
        """
        result = changelog_of(databases, "drift_changelog_filename")
        assert result.status is ChangelogStatus.IN_SYNC
        assert result.behind_count == 0
        assert result.ahead_count == 0

    def test_the_difference_is_still_recorded(self, databases):
        result = changelog_of(databases, "drift_changelog_filename")
        # Reported, just not as drift: the basename is unchanged, only the path.
        assert [d.ref for d in result.filename_differences] == [("add-invoice-line", "kolowae")]
        assert [d.severity for d in result.filename_differences] == [Severity.INFO]


class TestOrdering:
    def test_a_shifted_counter_base_is_not_drift(self, databases):
        """ORDEREXECUTED is a per-deployment counter.

        The fixture adds 100 to every value, which is what a differently-deployed environment looks
        like. Comparing the number would report the whole history as wrong.
        """
        result = changelog_of(databases, "drift_changelog_reordered")
        assert result.status is ChangelogStatus.IN_SYNC
        assert result.order_inversions == ()
        assert result.severity is Severity.INFO


class TestAbsence:
    def test_a_missing_table_in_the_target_is_an_error_with_a_hint(self, databases):
        result = changelog_of(databases, "drift_changelog_missing")
        assert result.status is ChangelogStatus.MISSING_IN_TARGET
        assert result.severity is Severity.ERROR
        assert "liquibase.schema" in result.headline("prod", "qa")

    def test_two_tables_are_reported_as_ambiguous_rather_than_guessed(self, databases):
        result = changelog_of(databases, "drift_changelog_ambiguous")
        assert result.status is ChangelogStatus.AMBIGUOUS
        assert result.severity is Severity.WARNING
        assert len(result.candidates) == 2

    def test_a_configured_location_resolves_the_ambiguity(self, databases):
        """Being explicit beats guessing.

        With two candidate tables the tool refuses to choose, but naming one in the config makes the
        comparison deterministic again.
        """
        from db_schema_comparer.build import build_inventory
        from db_schema_comparer.config.model import SourceRef
        from db_schema_comparer.config.secrets import Dsn
        from db_schema_comparer.db.connect import open_connection

        databases.setup("base", drift="drift_changelog_ambiguous")
        source = SourceRef(
            label="qa",
            dsn_env="X",
            liquibase={"schema": "cumo-invoicing", "table": "DATABASECHANGELOG"},
        )
        with open_connection(Dsn(databases.target_dsn, env_name="X"), label="qa") as connection:
            inventory = build_inventory(connection, source)
        assert inventory.changelog is not None
        assert inventory.changelog.location is not None
        assert inventory.changelog.location.schema == "cumo-invoicing"
        assert inventory.changelog.count == 3


class TestLock:
    def test_a_held_lock_is_reported_as_a_warning(self, databases):
        result = changelog_of(databases, "drift_changelog_locked")
        assert result.severity is Severity.WARNING
        assert any("deployment lock" in n for n in result.notes)


class TestStrictMode:
    def test_strict_mode_drops_the_loose_column_note(self, databases):
        databases.setup("base")
        loose = compare(databases).changelog
        strict = compare(databases, changelog_options=ChangelogOptions(strict=True)).changelog
        assert any("COMMENTS" in n for n in loose.notes)
        assert not any("COMMENTS" in n for n in strict.notes)


class TestDefaultIgnoresInteraction:
    def test_the_changelog_tables_ddl_is_suppressed_but_the_history_is_not(self, databases):
        """The bundled ruleset ignores the changelog *tables*.

        Their DDL is created by Liquibase and varies with its version. Their *contents* are the
        point of this whole section, so suppressing the tables must not suppress the comparison.
        """
        from db_schema_comparer.diff.engine import diff_inventories
        from db_schema_comparer.diff.ignores import IgnoreRuleSet, load_default_ignores

        databases.setup("base", drift="drift_changelog_behind")
        result = diff_inventories(
            inventory_of(databases.master_dsn, "prod"),
            inventory_of(databases.target_dsn, "qa"),
            ignores=IgnoreRuleSet.from_config(None, defaults=load_default_ignores()),
        )
        assert result.changelog is not None
        assert result.changelog.behind_count == 2
        assert result.worst_severity() is Severity.ERROR
