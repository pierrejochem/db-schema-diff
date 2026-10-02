"""The structured result of comparing two inventories.

Everything downstream — the console, JSON, JUnit and HTML reporters, and the exit code — reads
only these types. Nothing here knows about connections, configuration or the clock, so a
comparison can be rendered again later from a saved file and produce byte-identical output.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..model.inventory import SourceInfo
from ..model.keys import ObjectKey
from ..model.kinds import KIND_ORDER, ObjectKind
from .changelog import ChangelogDiff, ChangelogStatus
from .severity import Severity

#: Version of the JSON report format. Bumped whenever the shape changes incompatibly.
REPORT_SCHEMA_VERSION = 1

#: Versions this build can *read*; see the note on the inventory's equivalent.
READABLE_REPORT_VERSIONS = frozenset({1})


class ObjectStatus(StrEnum):
    """How one object compares between master and target."""

    MATCH = "match"
    MISSING_IN_TARGET = "missing_in_target"
    """In the master, absent from the target: the target is behind."""
    EXTRA_IN_TARGET = "extra_in_target"
    """In the target, absent from the master: the target has diverged."""
    DIFFERS = "differs"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ")


class NoteKind(StrEnum):
    """A statement about the comparison itself rather than about one object."""

    TARGET_EMPTY = "target_empty"
    """The target has no objects at all. Reported once instead of thousands of times."""
    VERSION_SKEW = "version_skew"
    """The two servers are different PostgreSQL majors, so printed definitions differ by
    formatting alone and body comparisons were downgraded."""
    COLLATION_SKEW = "collation_skew"
    """The two databases have different collations, so every text column's collation differs
    by definition and that attribute was suppressed."""
    PRIVILEGE_LIMITED = "privilege_limited"
    """A definition came back NULL, which means the connecting role cannot see the object —
    not that the object differs."""
    CHANGELOG_LOCKED = "changelog_locked"
    """A Liquibase deployment may have been in progress while the snapshot was taken."""
    CHANGELOG_AMBIGUOUS = "changelog_ambiguous"
    """More than one DATABASECHANGELOG table was found and none was configured."""
    PROBE_FAILED = "probe_failed"
    """A source could not be inspected."""
    BASELINE_APPLIED = "baseline_applied"
    """A baseline was supplied, so findings it already contains are not gating this run."""


@dataclass(frozen=True, slots=True)
class Note:
    """Something the reader needs to know to interpret the findings correctly."""

    kind: NoteKind
    message: str
    severity: Severity = Severity.INFO

    def to_json_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "message": self.message, "severity": self.severity.label}

    @classmethod
    def from_json_dict(cls, data: Mapping[str, Any]) -> Note:
        return cls(
            kind=NoteKind(data["kind"]),
            message=data["message"],
            severity=_severity(data["severity"]),
        )


@dataclass(frozen=True, slots=True)
class AttributeDelta:
    """One attribute that differs between the master's and the target's version of an object."""

    attribute: str
    master_value: str | None
    target_value: str | None
    severity: Severity
    cosmetic: bool = False
    """The raw texts differed but the canonical values matched.

    Recorded so ``--show-cosmetic`` can prove the normaliser is doing its job, and hidden by
    default because it is not drift.
    """
    note: str | None = None
    """Why this matters, when the attribute name alone does not say it."""
    master_display: str | None = None
    target_display: str | None = None
    """What to *show* instead of the compared values, when there is something better to show.

    A routine is compared by its body hash, which is also what identifies the finding in a
    baseline; these carry the body text for a reader. Both or neither, and purely additive:
    ``master_value`` and ``target_value`` remain the compared values.
    """

    def to_json_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "attribute": self.attribute,
            "master": self.master_value,
            "target": self.target_value,
            "severity": self.severity.label,
        }
        if self.cosmetic:
            payload["cosmetic"] = True
        if self.note:
            payload["note"] = self.note
        if self.master_display is not None and self.target_display is not None:
            payload["master_display"] = self.master_display
            payload["target_display"] = self.target_display
        return payload

    @classmethod
    def from_json_dict(cls, data: Mapping[str, Any]) -> AttributeDelta:
        return cls(
            attribute=data["attribute"],
            master_value=data.get("master"),
            target_value=data.get("target"),
            severity=_severity(data["severity"]),
            cosmetic=bool(data.get("cosmetic", False)),
            note=data.get("note"),
            master_display=data.get("master_display"),
            target_display=data.get("target_display"),
        )


@dataclass(frozen=True, slots=True)
class ObjectFinding:
    """One object's comparison result."""

    key: ObjectKey
    status: ObjectStatus
    severity: Severity
    deltas: tuple[AttributeDelta, ...] = ()
    paired_with: ObjectKey | None = None
    """The differently-named object this one was matched to by rename reconciliation."""
    ignored_by: str | None = None
    """Id of the ignore rule that suppressed or downgraded this. Kept, not erased, so an
    ignored finding is still auditable."""

    @property
    def kind(self) -> ObjectKind:
        return self.key.kind

    def to_json_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.key.kind.value,
            "path": self.key.path,
            "status": self.status.value,
            "severity": self.severity.label,
        }
        if self.deltas:
            payload["deltas"] = [d.to_json_dict() for d in self.deltas]
        if self.paired_with is not None:
            payload["paired_with"] = self.paired_with.path
        if self.ignored_by is not None:
            payload["ignored_by"] = self.ignored_by
        return payload

    @classmethod
    def from_json_dict(cls, data: Mapping[str, Any]) -> ObjectFinding:
        return cls(
            key=_key(data["kind"], data["path"]),
            status=ObjectStatus(data["status"]),
            severity=_severity(data["severity"]),
            deltas=tuple(AttributeDelta.from_json_dict(d) for d in data.get("deltas", ())),
            paired_with=(
                _key(data["kind"], data["paired_with"]) if data.get("paired_with") else None
            ),
            ignored_by=data.get("ignored_by"),
        )


@dataclass(frozen=True)
class TargetDiff:
    """The master compared against one target."""

    master_label: str
    target_label: str
    master_source: SourceInfo | None
    target_source: SourceInfo | None
    findings: tuple[ObjectFinding, ...] = ()
    ignored: tuple[ObjectFinding, ...] = ()
    notes: tuple[Note, ...] = ()
    changelog: ChangelogDiff | None = None
    """Liquibase migration history, when both sides were read.

    Kept separate from ``findings`` because it answers a different question: the findings say what
    differs, this says which environment is behind and where the histories split.
    """
    failed: str | None = None
    """Why this target could not be inspected, when it could not be."""

    @property
    def skipped(self) -> bool:
        return self.failed is not None

    def worst_severity(self) -> Severity | None:
        """Highest severity among findings, notes and the changelog verdict.

        ``None`` only when everything matched.
        """
        severities = [f.severity for f in self.findings] + [n.severity for n in self.notes]
        changelog = self.changelog
        # Any severity above INFO counts whatever the status says: a severity that exists must reach
        # the exit code, so a future status bug cannot silently discard it. A clean verdict carries
        # INFO, which is why INFO alone still needs a status other than IN_SYNC.
        if (
            changelog is not None
            and changelog.applicable
            and (
                changelog.status is not ChangelogStatus.IN_SYNC
                or changelog.severity > Severity.INFO
            )
        ):
            severities.append(changelog.severity)
        return max(severities) if severities else None

    def at_or_above(self, threshold: Severity) -> tuple[ObjectFinding, ...]:
        """Findings that meet or exceed ``threshold``."""
        return tuple(f for f in self.findings if f.severity >= threshold)

    def counts_by_kind(self) -> dict[ObjectKind, dict[ObjectStatus, int]]:
        """Finding counts per kind and status, in report order. Drives the HTML matrix."""
        table: dict[ObjectKind, dict[ObjectStatus, int]] = {}
        for finding in self.findings:
            per_kind = table.setdefault(finding.kind, {})
            per_kind[finding.status] = per_kind.get(finding.status, 0) + 1
        return {kind: table[kind] for kind in KIND_ORDER if kind in table}

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "target": self.target_label,
            "master": self.master_label,
            "failed": self.failed,
            "master_source": self.master_source.to_json_dict() if self.master_source else None,
            "target_source": self.target_source.to_json_dict() if self.target_source else None,
            "worst_severity": (w.label if (w := self.worst_severity()) else None),
            "findings": [f.to_json_dict() for f in self.findings],
            "ignored": [f.to_json_dict() for f in self.ignored],
            "notes": [n.to_json_dict() for n in self.notes],
            "changelog": self.changelog.to_json_dict() if self.changelog else None,
        }

    @classmethod
    def from_json_dict(cls, data: Mapping[str, Any]) -> TargetDiff:
        master = data.get("master_source")
        target = data.get("target_source")
        return cls(
            master_label=data["master"],
            target_label=data["target"],
            master_source=SourceInfo.from_json_dict(master) if master else None,
            target_source=SourceInfo.from_json_dict(target) if target else None,
            findings=tuple(ObjectFinding.from_json_dict(f) for f in data.get("findings", ())),
            ignored=tuple(ObjectFinding.from_json_dict(f) for f in data.get("ignored", ())),
            notes=tuple(Note.from_json_dict(n) for n in data.get("notes", ())),
            changelog=(_changelog_from_json(data["changelog"]) if data.get("changelog") else None),
            failed=data.get("failed"),
        )


@dataclass(frozen=True)
class ComparisonReport:
    """One master against every target. The only input every reporter takes."""

    name: str
    master_label: str
    targets: tuple[TargetDiff, ...] = ()
    notes: tuple[Note, ...] = ()
    generated_at: str = ""
    tool_version: str = ""
    fail_on: str = "error"
    probe_failed: bool = False
    """Whether any source could not be inspected.

    Kept distinct from drift: a partial comparison must never be reported as a clean gate.
    """

    def worst_severity(self) -> Severity | None:
        severities = [
            s for target in self.targets if (s := target.worst_severity()) is not None
        ] + [n.severity for n in self.notes]
        return max(severities) if severities else None

    def has_drift(self, threshold: Severity | None) -> bool:
        """Whether anything met the failure threshold."""
        if threshold is None:
            return False
        worst = self.worst_severity()
        return worst is not None and worst >= threshold

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "name": self.name,
            "master": self.master_label,
            "generated_at": self.generated_at,
            "tool_version": self.tool_version,
            "fail_on": self.fail_on,
            "probe_failed": self.probe_failed,
            "worst_severity": (w.label if (w := self.worst_severity()) else None),
            "notes": [n.to_json_dict() for n in self.notes],
            "targets": [t.to_json_dict() for t in self.targets],
        }

    @classmethod
    def from_json_dict(cls, data: Mapping[str, Any]) -> ComparisonReport:
        """Rebuild a report written by :meth:`to_json_dict`.

        This is what makes ``render --from report.json`` possible, and what lets a comparison be
        captured in one place and rendered somewhere else entirely.
        """
        version = data.get("schema_version")
        if type(version) is not int or version not in READABLE_REPORT_VERSIONS:
            hint = " (expected an integer)" if isinstance(version, str) else ""
            raise ValueError(
                f"unsupported report schema_version {version!r}; "
                f"this build reads {sorted(READABLE_REPORT_VERSIONS)} "
                f"and writes {REPORT_SCHEMA_VERSION}{hint}"
            )
        return cls(
            name=data["name"],
            master_label=data["master"],
            targets=tuple(TargetDiff.from_json_dict(t) for t in data.get("targets", ())),
            notes=tuple(Note.from_json_dict(n) for n in data.get("notes", ())),
            generated_at=data.get("generated_at", ""),
            tool_version=data.get("tool_version", ""),
            fail_on=data.get("fail_on", "error"),
            probe_failed=bool(data.get("probe_failed", False)),
        )


@dataclass(frozen=True, slots=True)
class DiffOptions:
    """What to compare, and how strictly."""

    ignore_column_order: bool = False
    include_owners: bool = False
    include_comments: bool = False
    include_grants: bool = False
    show_cosmetic: bool = False
    ignored_attributes: frozenset[str] = field(default_factory=frozenset)
    """Attributes suppressed for a reason discovered at run time, such as ``column.collation``
    when the two databases' collations differ."""
    downgrade_bodies: bool = False
    """Downgrade definition-body differences to INFO, set when the servers are different
    PostgreSQL majors."""


def _severity(label: str) -> Severity:
    """A severity from its lower-case label."""
    try:
        return Severity[label.upper()]
    except KeyError:
        raise ValueError(f"unknown severity {label!r}") from None


def _key(kind: str, path: str) -> ObjectKey:
    """Rebuild an object key from the ``schema.name[.subname]`` path in a report.

    Split from the left on the first two separators only, so a name containing a dot survives.
    A column path is three parts; anything else is two.
    """
    object_kind = ObjectKind(kind)
    parts = path.split(".")
    if object_kind is ObjectKind.COLUMN and len(parts) >= 3:
        return ObjectKey(object_kind, parts[0], ".".join(parts[1:-1]), parts[-1])
    if len(parts) < 2:
        raise ValueError(f"malformed object path {path!r}")
    return ObjectKey(object_kind, parts[0], ".".join(parts[1:]))


def _changelog_from_json(data: Mapping[str, Any]) -> ChangelogDiff:
    """Rebuild a changelog verdict from its JSON form.

    Only the fields a reporter reads are restored: the detail lists are display data, and rebuilding
    every nested record would add a lot of surface for no reader's benefit.
    """
    from .changelog import (
        ChecksumMismatch,
        ExecTypeDifference,
        FailedChangeset,
        FilenameDifference,
        OrderInversion,
    )

    def ref(pair: Any) -> tuple[str, str]:
        return (str(pair[0]), str(pair[1]))

    return ChangelogDiff(
        status=ChangelogStatus(data["status"]),
        severity=_severity(data["severity"]),
        master_count=int(data.get("master_count", 0)),
        target_count=int(data.get("target_count", 0)),
        missing_in_target=tuple(ref(r) for r in data.get("missing_in_target", ())),
        extra_in_target=tuple(ref(r) for r in data.get("extra_in_target", ())),
        checksum_mismatches=tuple(
            ChecksumMismatch(ref((m["id"], m["author"])), m.get("master"), m.get("target"))
            for m in data.get("checksum_mismatches", ())
        ),
        checksum_algorithm_skew=bool(data.get("checksum_algorithm_skew", False)),
        cleared_checksums=tuple(ref(r) for r in data.get("cleared_checksums", ())),
        exectype_differences=tuple(
            ExecTypeDifference(
                ref((d["id"], d["author"])), d["master"], d["target"], _severity(d["severity"])
            )
            for d in data.get("exectype_differences", ())
        ),
        filename_differences=tuple(
            FilenameDifference(
                ref((d["id"], d["author"])), d["master"], d["target"], _severity(d["severity"])
            )
            for d in data.get("filename_differences", ())
        ),
        order_inversions=tuple(
            OrderInversion(ref(i["first_in_master"]), ref(i["second_in_master"]))
            for i in data.get("order_inversions", ())
        ),
        # An entry is [id, author, side]; a report written before the side was recorded has two.
        failed_changesets=tuple(
            FailedChangeset(str(r[0]), str(r[1]), str(r[2]) if len(r) > 2 else "unknown")
            for r in data.get("failed_changesets", ())
        ),
        first_divergence=(ref(data["first_divergence"]) if data.get("first_divergence") else None),
        master_tag=data.get("master_tag"),
        target_tag=data.get("target_tag"),
        master_location=data.get("master_location"),
        target_location=data.get("target_location"),
        candidates=tuple(data.get("candidates", ())),
        notes=tuple(data.get("notes", ())),
    )
