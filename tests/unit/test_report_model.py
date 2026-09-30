"""Serialization of the comparison result.

The JSON form has to be lossless enough to regenerate the other reporters from it, so this pins
what it contains.
"""

from __future__ import annotations

import json

from cumo_schema_comparer.diff.model import (
    AttributeDelta,
    ComparisonReport,
    Note,
    NoteKind,
    ObjectFinding,
    ObjectStatus,
    TargetDiff,
)
from cumo_schema_comparer.diff.severity import FAIL_ON_THRESHOLDS, Severity, gate
from cumo_schema_comparer.model.keys import column_key, table_key
from cumo_schema_comparer.model.kinds import ObjectKind
from tests.support.builders import source


def finding(path_parts, status, severity, deltas=()):
    key = column_key(*path_parts) if len(path_parts) == 3 else table_key(*path_parts)
    return ObjectFinding(key=key, status=status, severity=severity, deltas=deltas)


def report():
    delta = AttributeDelta(
        attribute="column.data_type",
        master_value="int4",
        target_value="int8",
        severity=Severity.ERROR,
        note="a type change",
    )
    target = TargetDiff(
        master_label="prod",
        target_label="qa",
        master_source=source("prod"),
        target_source=source("qa"),
        findings=(
            finding(("public", "invoice", "id"), ObjectStatus.DIFFERS, Severity.ERROR, (delta,)),
            finding(("public", "tmp"), ObjectStatus.EXTRA_IN_TARGET, Severity.WARNING),
        ),
        ignored=(
            ObjectFinding(
                key=table_key("public", "QRTZ_LOCKS"),
                status=ObjectStatus.EXTRA_IN_TARGET,
                severity=Severity.WARNING,
                ignored_by="quartz-runtime",
            ),
        ),
        notes=(Note(kind=NoteKind.VERSION_SKEW, message="15 vs 17", severity=Severity.WARNING),),
    )
    return ComparisonReport(
        name="invoicing", master_label="prod", targets=(target,), fail_on="error"
    )


class TestJson:
    def test_the_payload_is_json_serializable(self):
        text = json.dumps(report().to_json_dict())
        assert json.loads(text)["name"] == "invoicing"

    def test_findings_deltas_notes_and_ignores_all_survive(self):
        payload = report().to_json_dict()["targets"][0]
        assert len(payload["findings"]) == 2
        assert payload["findings"][0]["deltas"][0]["master"] == "int4"
        assert payload["findings"][0]["deltas"][0]["severity"] == "error"
        assert payload["notes"][0]["kind"] == "version_skew"
        # An ignored finding stays in the payload: suppressed is not the same as invisible.
        assert payload["ignored"][0]["ignored_by"] == "quartz-runtime"

    def test_the_worst_severity_is_reported_at_both_levels(self):
        payload = report().to_json_dict()
        assert payload["worst_severity"] == "error"
        assert payload["targets"][0]["worst_severity"] == "error"

    def test_source_metadata_carries_no_credential(self):
        payload = json.dumps(report().to_json_dict())
        for forbidden in ("password", "dsn", "PROD_DSN"):
            assert forbidden not in payload


class TestAggregates:
    def test_counts_by_kind_follow_report_order(self):
        counts = report().targets[0].counts_by_kind()
        assert list(counts) == [ObjectKind.TABLE, ObjectKind.COLUMN]

    def test_at_or_above_filters_by_severity(self):
        target = report().targets[0]
        assert len(target.at_or_above(Severity.ERROR)) == 1
        assert len(target.at_or_above(Severity.WARNING)) == 2

    def test_a_fully_matching_target_has_no_worst_severity(self):
        empty = TargetDiff(
            master_label="prod", target_label="qa", master_source=None, target_source=None
        )
        assert empty.worst_severity() is None


class TestGate:
    def test_each_fail_on_value_maps_to_a_threshold(self):
        assert gate("error") is Severity.ERROR
        assert gate("warning") is Severity.WARNING
        assert gate("any") is Severity.INFO
        assert gate("never") is None
        assert set(FAIL_ON_THRESHOLDS) == {"error", "warning", "any", "never"}

    def test_drift_is_measured_against_the_threshold(self):
        r = report()
        assert r.has_drift(Severity.ERROR) is True
        assert r.has_drift(None) is False

    def test_a_warning_only_report_does_not_trip_the_error_gate(self):
        target = TargetDiff(
            master_label="prod",
            target_label="qa",
            master_source=None,
            target_source=None,
            findings=(finding(("public", "tmp"), ObjectStatus.EXTRA_IN_TARGET, Severity.WARNING),),
        )
        r = ComparisonReport(name="x", master_label="prod", targets=(target,))
        assert r.has_drift(Severity.ERROR) is False
        assert r.has_drift(Severity.WARNING) is True

    def test_an_unknown_fail_on_value_is_rejected(self):
        import pytest

        with pytest.raises(ValueError, match="fail_on"):
            gate("sometimes")
