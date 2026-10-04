"""A canned comparison report, for testing reporters without a database.

Deliberately covers every shape a reporter has to handle: a drifted target, a target in sync, a
target that could not be inspected, findings at all three severities, an ignored finding, notes,
and a value containing characters that break XML.
"""

from __future__ import annotations

from db_schema_diff.diff.model import (
    AttributeDelta,
    ComparisonReport,
    Note,
    NoteKind,
    ObjectFinding,
    ObjectStatus,
    TargetDiff,
)
from db_schema_diff.diff.severity import Severity
from db_schema_diff.model.keys import column_key, table_key

from .builders import source

#: A control character, as can legitimately appear in a column comment or a check expression.
#: XML 1.0 cannot represent it at all, escaped or not.
HOSTILE_TEXT = "bad\x01value"


def drifted_target() -> TargetDiff:
    return TargetDiff(
        master_label="prod",
        target_label="qa",
        master_source=source("prod"),
        target_source=source("qa"),
        findings=(
            ObjectFinding(
                key=table_key("acme-invoicing", "DunningLevel"),
                status=ObjectStatus.MISSING_IN_TARGET,
                severity=Severity.ERROR,
            ),
            ObjectFinding(
                key=column_key("acme-invoicing", "invoice", "number"),
                status=ObjectStatus.DIFFERS,
                severity=Severity.ERROR,
                deltas=(
                    AttributeDelta(
                        attribute="column.data_type",
                        master_value="varchar(40)",
                        target_value="text",
                        severity=Severity.ERROR,
                        note="a type change breaks anything relying on the length",
                    ),
                ),
            ),
            ObjectFinding(
                key=column_key("acme-invoicing", "invoice", "status"),
                status=ObjectStatus.DIFFERS,
                severity=Severity.WARNING,
                deltas=(
                    AttributeDelta(
                        attribute="column.default",
                        master_value="'DRAFT'",
                        target_value=HOSTILE_TEXT,
                        severity=Severity.WARNING,
                    ),
                ),
            ),
            ObjectFinding(
                key=table_key("public", "tmp_debug"),
                status=ObjectStatus.EXTRA_IN_TARGET,
                severity=Severity.WARNING,
            ),
            ObjectFinding(
                key=table_key("public", "audit_entry"),
                status=ObjectStatus.DIFFERS,
                severity=Severity.INFO,
                deltas=(
                    AttributeDelta(
                        attribute="table.reloptions",
                        master_value="fillfactor=90",
                        target_value=None,
                        severity=Severity.INFO,
                    ),
                ),
            ),
        ),
        ignored=(
            ObjectFinding(
                key=table_key("public", "QRTZ_LOCKS"),
                status=ObjectStatus.EXTRA_IN_TARGET,
                severity=Severity.WARNING,
                ignored_by="quartz-runtime",
            ),
        ),
        notes=(
            Note(
                kind=NoteKind.VERSION_SKEW,
                message="prod runs PostgreSQL 15 and qa runs 17",
                severity=Severity.WARNING,
            ),
        ),
    )


def in_sync_target() -> TargetDiff:
    return TargetDiff(
        master_label="prod",
        target_label="dev",
        master_source=source("prod"),
        target_source=source("dev"),
    )


def skipped_target() -> TargetDiff:
    return TargetDiff(
        master_label="prod",
        target_label="local",
        master_source=source("prod"),
        target_source=None,
        failed="local: cannot connect (host=127.0.0.1, port=1)",
    )


def full_report(*, fail_on: str = "error") -> ComparisonReport:
    """A report with a drifted, an in-sync and an unreachable target."""
    return ComparisonReport(
        name="invoicing",
        master_label="prod",
        targets=(drifted_target(), in_sync_target(), skipped_target()),
        notes=(
            Note(
                kind=NoteKind.PROBE_FAILED,
                message="target 'local' could not be inspected",
                severity=Severity.WARNING,
            ),
        ),
        generated_at="2026-01-15T09:30:00+00:00",
        tool_version="0.1.0",
        fail_on=fail_on,
        probe_failed=True,
    )


def clean_report() -> ComparisonReport:
    """A report with nothing wrong, for the in-sync rendering path."""
    return ComparisonReport(
        name="invoicing",
        master_label="prod",
        targets=(in_sync_target(),),
        generated_at="2026-01-15T09:30:00+00:00",
        tool_version="0.1.0",
    )
