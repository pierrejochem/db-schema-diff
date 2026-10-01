"""Comparing Liquibase migration history.

A pure function of two states, so every case here is two small histories differing in one way.

The cases that matter most are the ones where a naive comparison would be *confidently wrong*: a
Liquibase upgrade that rewrote every checksum, an ``ORDEREXECUTED`` counter compared numerically, or
a ``MARK_RAN`` precondition treated as a failure.
"""

from __future__ import annotations

import pytest

from cumo_schema_comparer.diff.changelog import (
    ChangelogOptions,
    ChangelogStatus,
    diff_changelog,
)
from cumo_schema_comparer.diff.severity import Severity
from cumo_schema_comparer.model.changelog import ChangelogState
from tests.support.builders import changelog, changeset


def history(*specs, **kwargs) -> ChangelogState:
    """A changelog from ``(id, md5, exectype)``-ish specs, numbered in order."""
    rows = []
    for order, spec in enumerate(specs, start=1):
        if isinstance(spec, str):
            rows.append(changeset(spec, order=order, md5sum="9:" + spec))
        else:
            rows.append(spec)
    return changelog(*rows, **kwargs)


def cs(
    id_, *, order=1, md5="9:aaa", exec_type="EXECUTED", filename=None, tag=None, author="kolowae"
):
    return changeset(
        id_,
        author=author,
        order=order,
        md5sum=md5,
        exec_type=exec_type,
        filename=filename or "liquibase/update.xml",
        tag=tag,
    )


class TestInSync:
    def test_identical_histories_are_in_sync(self):
        result = diff_changelog(history("a", "b", "c"), history("a", "b", "c"))
        assert result.status is ChangelogStatus.IN_SYNC
        assert result.severity is Severity.INFO
        assert result.behind_count == 0

    def test_the_counts_are_reported(self):
        result = diff_changelog(history("a", "b"), history("a", "b"))
        assert (result.master_count, result.target_count) == (2, 2)

    def test_the_location_is_reported_for_both_sides(self):
        result = diff_changelog(history("a"), history("a"))
        # Proves the changelog was located rather than assumed, and where.
        assert result.master_location == "cumo-invoicing.DATABASECHANGELOG"
        assert result.target_location == "cumo-invoicing.DATABASECHANGELOG"


class TestBehindAndAhead:
    def test_a_target_missing_changesets_is_behind(self):
        result = diff_changelog(history("a", "b", "c"), history("a"))
        assert result.status is ChangelogStatus.TARGET_BEHIND
        assert result.severity is Severity.ERROR
        assert result.behind_count == 2
        assert result.missing_in_target == (("b", "kolowae"), ("c", "kolowae"))

    def test_a_target_with_extra_changesets_is_ahead(self):
        result = diff_changelog(history("a"), history("a", "b"))
        assert result.status is ChangelogStatus.TARGET_AHEAD
        # Usually a lower environment testing an unreleased migration, not a broken deployment.
        assert result.severity is Severity.WARNING
        assert result.ahead_count == 1

    def test_both_directions_at_once_is_divergence(self):
        result = diff_changelog(history("a", "b"), history("a", "c"))
        assert result.status is ChangelogStatus.DIVERGED
        assert result.severity is Severity.ERROR

    def test_the_headline_names_the_first_missing_changeset(self):
        result = diff_changelog(history("a", "b", "c"), history("a"))
        headline = result.headline("prod", "qa")
        assert "qa is 2 changeset(s) behind prod" in headline
        assert "first missing: b by kolowae" in headline


class TestIdentity:
    def test_identity_is_id_and_author(self):
        master = changelog(cs("same", author="alice"))
        target = changelog(cs("same", author="bob"))
        # Different authors are different changesets, which is how Liquibase itself sees them.
        result = diff_changelog(master, target)
        assert result.status is ChangelogStatus.DIVERGED

    def test_filename_is_not_part_of_identity(self):
        """cumo-invoicing's master.xml mixes relative and non-relative includes.

        Some of its changelogs declare their own logicalFilePath, so the same changeset legitimately
        records a different filename in different deployments. Including it in the identity would
        report every changeset as both missing and extra.
        """
        master = changelog(cs("a", filename="liquibase/update_2026.xml"))
        target = changelog(cs("a", filename="update_2026.xml"))
        result = diff_changelog(master, target)
        assert result.status is ChangelogStatus.IN_SYNC
        assert result.behind_count == 0

    def test_a_different_path_to_the_same_file_is_only_informational(self):
        master = changelog(cs("a", filename="liquibase/update_2026.xml"))
        target = changelog(cs("a", filename="db/liquibase/update_2026.xml"))
        result = diff_changelog(master, target)
        assert [d.severity for d in result.filename_differences] == [Severity.INFO]

    def test_a_different_basename_is_a_warning(self):
        # A changeset that moved between files is worth a look.
        master = changelog(cs("a", filename="liquibase/initial.xml"))
        target = changelog(cs("a", filename="liquibase/update_2026.xml"))
        result = diff_changelog(master, target)
        assert [d.severity for d in result.filename_differences] == [Severity.WARNING]


class TestChecksums:
    def test_a_real_content_change_is_an_error(self):
        master = changelog(cs("a", md5="9:aaaa"))
        target = changelog(cs("a", md5="9:bbbb"))
        result = diff_changelog(master, target)
        assert result.status is ChangelogStatus.CHECKSUM_MISMATCH
        assert result.severity is Severity.ERROR
        assert len(result.checksum_mismatches) == 1

    def test_an_algorithm_upgrade_is_reported_once_not_per_changeset(self):
        """Liquibase prefixes every checksum with its algorithm version and rewrites all of them.

        Comparing the raw strings across an upgrade reports every changeset in the database as
        edited, which buries whatever is actually wrong.
        """
        master = changelog(cs("a", md5="8:aaaa", order=1), cs("b", md5="8:bbbb", order=2))
        target = changelog(cs("a", md5="9:cccc", order=1), cs("b", md5="9:dddd", order=2))
        result = diff_changelog(master, target)
        assert result.checksum_algorithm_skew is True
        assert result.checksum_mismatches == ()
        # Not IN_SYNC: a verdict carrying a WARNING must not be labelled in sync.
        assert result.status is ChangelogStatus.HISTORY_DIFFERS
        assert result.severity is Severity.WARNING
        assert any("algorithm versions" in n for n in result.notes)

    def test_a_matching_prefix_still_compares_the_digest(self):
        master = changelog(cs("a", md5="9:aaaa"))
        target = changelog(cs("a", md5="9:bbbb"))
        assert diff_changelog(master, target).checksum_mismatches

    def test_a_cleared_checksum_is_a_warning_not_a_mismatch(self):
        # clearCheckSums was run, so nothing can be verified about content either way.
        master = changelog(cs("a", md5="9:aaaa"))
        target = changelog(cs("a", md5=None))
        result = diff_changelog(master, target)
        assert result.cleared_checksums == (("a", "kolowae"),)
        assert result.checksum_mismatches == ()
        assert result.severity is Severity.WARNING

    def test_both_sides_cleared_is_not_reported(self):
        master = changelog(cs("a", md5=None))
        target = changelog(cs("a", md5=None))
        result = diff_changelog(master, target)
        assert result.cleared_checksums == ()

    def test_an_unprefixed_checksum_is_compared_whole(self):
        master = changelog(cs("a", md5="aaaa"))
        target = changelog(cs("a", md5="bbbb"))
        assert diff_changelog(master, target).checksum_mismatches


class TestExecType:
    def test_executed_versus_mark_ran_is_a_warning(self):
        """MARK_RAN is in active use in this platform.

        cumo-invoicing's update_20260423.xml gates a changeset on onFail="MARK_RAN" against row
        data, so the same changeset legitimately runs in one environment and is marked-ran in
        another. Worth reading; not a failed deployment.
        """
        master = changelog(cs("a", exec_type="EXECUTED"))
        target = changelog(cs("a", exec_type="MARK_RAN"))
        result = diff_changelog(master, target)
        assert [d.severity for d in result.exectype_differences] == [Severity.WARNING]
        assert result.severity is Severity.WARNING

    def test_the_warning_applies_in_either_direction(self):
        master = changelog(cs("a", exec_type="MARK_RAN"))
        target = changelog(cs("a", exec_type="EXECUTED"))
        assert [d.severity for d in diff_changelog(master, target).exectype_differences] == [
            Severity.WARNING
        ]

    def test_a_failed_changeset_is_an_error(self):
        master = changelog(cs("a", exec_type="EXECUTED"))
        target = changelog(cs("a", exec_type="FAILED"))
        result = diff_changelog(master, target)
        assert result.severity is Severity.ERROR
        assert [f.ref for f in result.failed_changesets] == [("a", "kolowae")]
        assert [f.side for f in result.failed_changesets] == ["target"]

    def test_reran_is_only_informational(self):
        master = changelog(cs("a", exec_type="EXECUTED"))
        target = changelog(cs("a", exec_type="RERAN"))
        result = diff_changelog(master, target)
        assert [d.severity for d in result.exectype_differences] == [Severity.INFO]
        assert result.severity is Severity.INFO


class TestOrdering:
    def test_order_executed_is_never_compared_numerically(self):
        """The same changeset is row 7 in one environment and row 12 in another.

        ORDEREXECUTED is a per-deployment counter, so comparing it as a number reports every
        changeset after any divergence as wrong.
        """
        master = changelog(cs("a", order=1), cs("b", order=2))
        target = changelog(cs("a", order=11), cs("b", order=12))
        result = diff_changelog(master, target)
        assert result.status is ChangelogStatus.IN_SYNC
        assert result.order_inversions == ()

    def test_a_genuine_relative_inversion_is_a_warning(self):
        master = changelog(cs("a", order=1), cs("b", order=2))
        target = changelog(cs("b", order=1), cs("a", order=2))
        result = diff_changelog(master, target)
        assert result.order_inversions
        assert result.severity is Severity.WARNING

    def test_a_changeset_only_on_one_side_does_not_create_an_inversion(self):
        master = changelog(cs("a", order=1), cs("b", order=2), cs("c", order=3))
        target = changelog(cs("a", order=1), cs("c", order=2))
        result = diff_changelog(master, target)
        # b is missing, which is reported as such; the rest is in the same relative order.
        assert result.order_inversions == ()
        assert result.behind_count == 1


class TestFirstDivergence:
    def test_it_points_at_the_earliest_problem_in_the_masters_own_order(self):
        master = changelog(cs("a", order=1), cs("b", order=2), cs("c", order=3))
        target = changelog(cs("a", order=1))
        assert diff_changelog(master, target).first_divergence == ("b", "kolowae")

    def test_a_checksum_mismatch_counts_as_divergence(self):
        master = changelog(cs("a", md5="9:aaa", order=1), cs("b", order=2))
        target = changelog(cs("a", md5="9:zzz", order=1), cs("b", order=2))
        assert diff_changelog(master, target).first_divergence == ("a", "kolowae")

    def test_an_earlier_mismatch_wins_over_a_later_missing_changeset(self):
        master = changelog(cs("a", md5="9:aaa", order=1), cs("b", order=2), cs("c", order=3))
        target = changelog(cs("a", md5="9:zzz", order=1), cs("b", order=2))
        assert diff_changelog(master, target).first_divergence == ("a", "kolowae")

    def test_none_when_nothing_diverged(self):
        assert diff_changelog(history("a"), history("a")).first_divergence is None

    def test_an_extra_changeset_alone_is_not_a_divergence_point(self):
        # The master's history is intact; the target simply has more.
        result = diff_changelog(history("a"), history("a", "b"))
        assert result.first_divergence is None


class TestTags:
    def test_the_tag_line_names_both_releases(self):
        master = changelog(cs("a", order=1, tag="R7.6.2"))
        target = changelog(cs("a", order=1, tag="R7.6.1"))
        assert diff_changelog(master, target).tag_line("prod", "qa") == (
            "prod @ R7.6.2 · qa @ R7.6.1"
        )

    def test_the_most_recent_tag_wins(self):
        master = changelog(cs("a", order=1, tag="R7.6.1"), cs("b", order=2, tag="R7.6.2"))
        assert diff_changelog(master, master).master_tag == "R7.6.2"

    def test_an_untagged_side_says_so(self):
        master = changelog(cs("a", order=1, tag="R7.6.2"))
        target = changelog(cs("a", order=1))
        assert "qa @ untagged" in (diff_changelog(master, target).tag_line("prod", "qa") or "")

    def test_no_tags_at_all_produces_no_line(self):
        assert diff_changelog(history("a"), history("a")).tag_line("prod", "qa") is None


class TestAbsence:
    """Every way a changelog can be missing. None of them may crash the run."""

    def test_missing_in_the_target_is_an_error(self):
        result = diff_changelog(history("a"), ChangelogState(location=None))
        assert result.status is ChangelogStatus.MISSING_IN_TARGET
        assert result.severity is Severity.ERROR
        # The hint matters: the table is often present but in an unexpected schema.
        assert "liquibase.schema" in result.headline("prod", "qa")

    def test_missing_in_the_master_is_only_a_warning(self):
        result = diff_changelog(ChangelogState(location=None), history("a"))
        assert result.status is ChangelogStatus.MISSING_IN_MASTER
        assert result.severity is Severity.WARNING

    def test_missing_in_both_is_not_applicable_rather_than_wrong(self):
        # Plenty of databases are not Liquibase-managed.
        result = diff_changelog(ChangelogState(location=None), ChangelogState(location=None))
        assert result.status is ChangelogStatus.MISSING_IN_BOTH
        assert result.severity is Severity.INFO
        assert result.applicable is False

    def test_a_none_state_is_treated_as_absent(self):
        assert diff_changelog(None, None).status is ChangelogStatus.MISSING_IN_BOTH

    def test_ambiguity_is_reported_rather_than_guessed(self):
        """Two changelog tables and no configured choice.

        Picking one arbitrarily would make the answer depend on catalog ordering, which is exactly
        the kind of non-determinism that makes a report untrustworthy.
        """
        from cumo_schema_comparer.model.changelog import ChangelogLocation

        ambiguous = ChangelogState(
            location=None,
            candidates=(
                ChangelogLocation("public", "DATABASECHANGELOG"),
                ChangelogLocation("cumo-invoicing", "DATABASECHANGELOG"),
            ),
        )
        result = diff_changelog(ambiguous, history("a"))
        assert result.status is ChangelogStatus.AMBIGUOUS
        assert result.severity is Severity.WARNING
        assert "liquibase.schema" in result.headline("prod", "qa")
        assert len(result.candidates) == 2


class TestLock:
    def test_a_held_lock_on_the_target_is_a_warning(self):
        master = changelog(cs("a"), lock_held=False)
        target = changelog(cs("a"), lock_held=True)
        result = diff_changelog(master, target)
        # The snapshot may have been taken mid-migration, which a reader needs to know before
        # trusting any of the findings.
        assert result.severity is Severity.WARNING
        assert any("deployment lock" in n for n in result.notes)

    def test_no_lock_produces_no_note(self):
        result = diff_changelog(history("a"), history("a"))
        assert not any("deployment lock" in n for n in result.notes)


class TestStrictMode:
    def test_the_loose_columns_are_noted_as_excluded_by_default(self):
        result = diff_changelog(history("a"), history("a"))
        assert any("COMMENTS" in n for n in result.notes)

    def test_strict_mode_drops_that_note(self):
        result = diff_changelog(history("a"), history("a"), options=ChangelogOptions(strict=True))
        assert not any("COMMENTS" in n for n in result.notes)


class TestSerialization:
    def test_the_verdict_round_trips_through_json(self):
        import json

        from cumo_schema_comparer.diff.model import _changelog_from_json

        master = changelog(cs("a", md5="9:aaa", order=1), cs("b", order=2, tag="R1"))
        target = changelog(cs("a", md5="9:zzz", order=1))
        original = diff_changelog(master, target)
        restored = _changelog_from_json(json.loads(json.dumps(original.to_json_dict())))

        assert restored.status is original.status
        assert restored.severity is original.severity
        assert restored.missing_in_target == original.missing_in_target
        assert restored.first_divergence == original.first_divergence
        assert restored.master_tag == original.master_tag
        assert [m.ref for m in restored.checksum_mismatches] == [
            m.ref for m in original.checksum_mismatches
        ]


@pytest.mark.parametrize(
    ("master_ids", "target_ids", "expected"),
    [
        (("a",), ("a",), ChangelogStatus.IN_SYNC),
        (("a", "b"), ("a",), ChangelogStatus.TARGET_BEHIND),
        (("a",), ("a", "b"), ChangelogStatus.TARGET_AHEAD),
        (("a", "b"), ("a", "c"), ChangelogStatus.DIVERGED),
    ],
)
def test_status_matrix(master_ids, target_ids, expected):
    assert diff_changelog(history(*master_ids), history(*target_ids)).status is expected
