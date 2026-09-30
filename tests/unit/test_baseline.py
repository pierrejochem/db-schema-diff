"""Accepting a known backlog.

The adoption problem this solves: a first run against environments that have drifted for years
produces hundreds of true findings, none of them actionable today. Without a way to accept them, the
report gets rejected and nobody runs it again.
"""

from __future__ import annotations

from dataclasses import replace

from cumo_schema_comparer.baseline import apply_baseline, signature
from cumo_schema_comparer.diff.model import (
    AttributeDelta,
    ComparisonReport,
    NoteKind,
    ObjectFinding,
    ObjectStatus,
    TargetDiff,
)
from cumo_schema_comparer.diff.severity import Severity, gate
from cumo_schema_comparer.model.keys import column_key, table_key


def delta(master="int4", target="int8"):
    return AttributeDelta(
        attribute="column.data_type",
        master_value=master,
        target_value=target,
        severity=Severity.ERROR,
    )


def finding(path=("public", "invoice", "amount"), status=ObjectStatus.DIFFERS, deltas=None):
    key = column_key(*path) if len(path) == 3 else table_key(*path)
    return ObjectFinding(
        key=key,
        status=status,
        severity=Severity.ERROR,
        deltas=(delta(),) if deltas is None else deltas,
    )


def report(*findings, target="qa"):
    return ComparisonReport(
        name="invoicing",
        master_label="prod",
        targets=(
            TargetDiff(
                master_label="prod",
                target_label=target,
                master_source=None,
                target_source=None,
                findings=tuple(findings),
            ),
        ),
    )


class TestAcceptingKnownFindings:
    def test_a_finding_in_the_baseline_stops_gating(self):
        known = finding()
        result = apply_baseline(report(known), report(known))
        assert result.accepted == 1
        assert result.new == 0
        assert result.report.targets[0].findings == ()
        assert result.report.has_drift(gate("error")) is False

    def test_an_accepted_finding_is_recorded_not_dropped(self):
        # It moves where ignore rules put theirs: still in the JSON, still a skipped JUnit case.
        known = finding()
        result = apply_baseline(report(known), report(known))
        ignored = result.report.targets[0].ignored
        assert [f.ignored_by for f in ignored] == ["baseline"]
        assert ignored[0].key == known.key

    def test_a_new_finding_still_fails(self):
        known = finding()
        fresh = finding(path=("public", "invoice", "number"))
        result = apply_baseline(report(known, fresh), report(known))
        assert result.new == 1
        assert [f.key.path for f in result.report.targets[0].findings] == ["public.invoice.number"]
        assert result.report.has_drift(gate("error")) is True

    def test_an_empty_baseline_accepts_nothing(self):
        result = apply_baseline(report(finding()), report())
        assert (result.accepted, result.new) == (0, 1)


class TestMatching:
    def test_the_same_object_on_a_different_target_is_not_accepted(self):
        # Each environment's backlog is its own.
        known = finding()
        result = apply_baseline(report(known, target="dev"), report(known, target="qa"))
        assert result.new == 1

    def test_a_changed_status_is_a_new_finding(self):
        known = finding(status=ObjectStatus.DIFFERS)
        now_missing = finding(status=ObjectStatus.MISSING_IN_TARGET, deltas=())
        result = apply_baseline(report(now_missing), report(known))
        assert result.new == 1

    def test_the_same_column_drifting_further_is_a_new_finding(self):
        """The delta values are part of the identity.

        A column that was already the wrong type stays accepted; that same column changing type
        *again* has to be caught, or the baseline would permanently blind the report to it.
        """
        known = finding(deltas=(delta("int4", "int8"),))
        worse = finding(deltas=(delta("int4", "text"),))
        result = apply_baseline(report(worse), report(known))
        assert result.new == 1

    def test_re_grading_a_severity_does_not_un_accept_a_backlog(self):
        # Severity is deliberately excluded from the signature: changing an attribute's grade later
        # must not silently invalidate every accepted finding.
        known = finding()
        regraded = replace(known, severity=Severity.WARNING)
        result = apply_baseline(report(regraded), report(known))
        assert result.accepted == 1

    def test_a_signature_covers_report_target_kind_path_status_and_deltas(self):
        assert signature("invoicing", "qa", finding()) == (
            "invoicing",
            "qa",
            "column",
            "public.invoice.amount",
            "differs",
            (("column.data_type", "int4", "int8"),),
        )

    def test_another_services_baseline_does_not_accept_this_ones_findings(self):
        """``--config`` can name a directory, so one run produces several reports.

        Two services can both have a target called ``qa`` and a table called ``public.audit``.
        Without scoping by report name, one service's backlog would silently accept the other's.
        """
        known = finding()
        other_service = replace(report(known), name="payment")
        result = apply_baseline(report(known), other_service)
        assert result.accepted == 0
        assert result.new == 1

    def test_a_baselines_own_ignored_findings_also_count_as_known(self):
        # Otherwise regenerating a baseline from a run that used ignore rules would lose them.
        known = finding()
        base = ComparisonReport(
            name="invoicing",
            master_label="prod",
            targets=(
                TargetDiff(
                    master_label="prod",
                    target_label="qa",
                    master_source=None,
                    target_source=None,
                    ignored=(replace(known, ignored_by="quartz-runtime"),),
                ),
            ),
        )
        assert apply_baseline(report(known), base).accepted == 1


class TestReporting:
    def test_a_note_summarises_what_the_baseline_did(self):
        known = finding()
        result = apply_baseline(report(known), report(known))
        notes = [n for n in result.report.notes if n.kind is NoteKind.BASELINE_APPLIED]
        assert notes
        assert "1 finding(s) accepted" in notes[0].message

    def test_findings_that_no_longer_occur_are_counted(self):
        """Worth saying out loud: the baseline can shrink.

        Somebody fixed something, and the baseline should be regenerated smaller rather than
        carrying an accepted finding forever.
        """
        gone = finding(
            path=("public", "old_table"), status=ObjectStatus.MISSING_IN_TARGET, deltas=()
        )
        result = apply_baseline(report(), report(gone))
        assert result.resolved == 1
        note = next(n for n in result.report.notes if n.kind is NoteKind.BASELINE_APPLIED)
        assert "no longer occur" in note.message

    def test_the_note_is_informational_and_does_not_gate(self):
        known = finding()
        result = apply_baseline(report(known), report(known))
        assert result.report.worst_severity() is Severity.INFO
        assert result.report.has_drift(gate("error")) is False
