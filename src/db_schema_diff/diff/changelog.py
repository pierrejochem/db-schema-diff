"""Comparing Liquibase migration history.

The DDL diff says *what* differs. This says *why*, and which environment is behind — which is the
question somebody actually has when a deployment misbehaves.

A pure function of two :class:`ChangelogState` values. No IO, no clock.

Four decisions here are load-bearing, and each was checked against this platform rather than
assumed:

**Identity is ``(ID, AUTHOR)``, excluding FILENAME.** ``acme-invoicing``'s ``master.xml`` mixes
relative and non-relative includes, and some changelogs declare their own ``logicalFilePath``, so
the same changeset legitimately records a different filename in different deployments. Including it
would report every changeset as both missing and extra.

**A checksum carries an algorithm-version prefix.** Liquibase writes ``8:`` or ``9:`` in front of
every digest and rewrites all of them when it upgrades. Comparing the raw strings across an upgrade
reports every changeset as mismatched, so a prefix difference is reported once as a Liquibase
version skew instead.

**``ORDEREXECUTED`` is a per-deployment counter.** It is never comparable numerically between
servers — the same changeset is row 7 in one environment and row 12 in another purely because of
deployment history. Only the *relative* order of changesets present in both is compared.

**``MARK_RAN`` is normal here.** ``acme-invoicing``'s ``update_20260423.xml`` gates a changeset on
``onFail="MARK_RAN"`` against row data, so the same changeset legitimately runs in one environment
and is marked-ran in another. That is a warning worth reading, not a failed deployment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import StrEnum
from typing import Any, NamedTuple

from ..model.changelog import ChangelogState, ChangeSetRef, ChangeSetRow, ExecType
from .severity import Severity

#: Columns whose differences are noise unless ``--strict-changelog`` is passed: they are rewritten
#: by ordinary changelog refactoring without the deployed schema changing at all.
LOOSE_COLUMNS = ("COMMENTS", "CONTEXTS", "LABELS")


class ChangelogStatus(StrEnum):
    """The headline verdict on one target's migration history."""

    IN_SYNC = "in_sync"
    TARGET_BEHIND = "target_behind"
    """The target is missing changesets the master has. The common case, and the actionable one."""
    TARGET_AHEAD = "target_ahead"
    """The target has changesets the master does not — usually a lower environment being used to
    test an unreleased migration."""
    DIVERGED = "diverged"
    """Each has changesets the other lacks. The histories genuinely split."""
    CHECKSUM_MISMATCH = "checksum_mismatch"
    """A changeset that ran in both has different content. Somebody edited a deployed changeset."""
    MISSING_IN_TARGET = "missing_in_target"
    """No changelog table in the target, though the master has one."""
    MISSING_IN_MASTER = "missing_in_master"
    MISSING_IN_BOTH = "missing_in_both"
    """Neither database is Liquibase-managed. Not an error; the section is simply not applicable."""
    AMBIGUOUS = "ambiguous"
    """Several changelog tables were found and none was configured."""
    FAILED_CHANGESETS = "failed_changesets"
    """The histories match by presence, but a changeset is recorded as FAILED: a migration broke."""
    LOCK_HELD = "lock_held"
    """The histories match, but a deployment lock is held, so a migration may be in progress."""
    HISTORY_DIFFERS = "history_differs"
    """The same changesets are present, but their order, execution type or filename differ."""

    @property
    def label(self) -> str:
        return self.value.replace("_", " ")


@dataclass(frozen=True, slots=True)
class ChecksumMismatch:
    """A changeset that ran in both databases with different content."""

    ref: ChangeSetRef
    master_checksum: str | None
    target_checksum: str | None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "id": self.ref[0],
            "author": self.ref[1],
            "master": self.master_checksum,
            "target": self.target_checksum,
        }


@dataclass(frozen=True, slots=True)
class ExecTypeDifference:
    """A changeset recorded with a different execution type on each side."""

    ref: ChangeSetRef
    master_exec_type: str
    target_exec_type: str
    severity: Severity

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "id": self.ref[0],
            "author": self.ref[1],
            "master": self.master_exec_type,
            "target": self.target_exec_type,
            "severity": self.severity.label,
        }


@dataclass(frozen=True, slots=True)
class FilenameDifference:
    """A changeset recorded under a different filename."""

    ref: ChangeSetRef
    master_filename: str
    target_filename: str
    severity: Severity

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "id": self.ref[0],
            "author": self.ref[1],
            "master": self.master_filename,
            "target": self.target_filename,
            "severity": self.severity.label,
        }


@dataclass(frozen=True, slots=True)
class OrderInversion:
    """Two changesets applied in opposite relative order on each side."""

    earlier_in_master: ChangeSetRef
    later_in_master: ChangeSetRef

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "first_in_master": list(self.earlier_in_master),
            "second_in_master": list(self.later_in_master),
        }


class FailedChangeset(NamedTuple):
    """A changeset recorded as FAILED, and on which side.

    A tuple of ``(id, author, side)`` so code that only reads the first two items keeps working.
    """

    id: str
    author: str
    side: str
    """``"master"``, ``"target"``, or ``"unknown"`` when read from a report written before the
    side was recorded."""

    @property
    def ref(self) -> ChangeSetRef:
        return (self.id, self.author)


@dataclass(frozen=True)
class ChangelogDiff:
    """One target's migration history compared against the master's."""

    status: ChangelogStatus
    severity: Severity = Severity.INFO

    master_count: int = 0
    target_count: int = 0
    missing_in_target: tuple[ChangeSetRef, ...] = ()
    extra_in_target: tuple[ChangeSetRef, ...] = ()
    checksum_mismatches: tuple[ChecksumMismatch, ...] = ()
    checksum_algorithm_skew: bool = False
    """The two sides' checksums were produced by different Liquibase algorithm versions.

    Reported once. Without this, a Liquibase upgrade makes every single changeset look edited.
    """
    cleared_checksums: tuple[ChangeSetRef, ...] = ()
    """Changesets with a NULL checksum, i.e. ``clearCheckSums`` was run. Nothing can be verified
    about their content."""
    exectype_differences: tuple[ExecTypeDifference, ...] = ()
    filename_differences: tuple[FilenameDifference, ...] = ()
    order_inversions: tuple[OrderInversion, ...] = ()
    failed_changesets: tuple[FailedChangeset, ...] = ()
    first_divergence: ChangeSetRef | None = None
    """The earliest master changeset that is missing, mismatched or differently executed.

    The most actionable field in the whole report: it points at where the two histories split,
    rather than listing every consequence of the split.
    """
    master_tag: str | None = None
    target_tag: str | None = None
    master_location: str | None = None
    target_location: str | None = None
    candidates: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def behind_count(self) -> int:
        return len(self.missing_in_target)

    @property
    def ahead_count(self) -> int:
        return len(self.extra_in_target)

    @property
    def applicable(self) -> bool:
        return self.status is not ChangelogStatus.MISSING_IN_BOTH

    def headline(self, master_label: str, target_label: str) -> str:
        """One sentence a reader can act on."""
        if self.status is ChangelogStatus.MISSING_IN_BOTH:
            return "neither database has a Liquibase changelog"
        if self.status is ChangelogStatus.MISSING_IN_TARGET:
            return (
                f"{target_label} has no Liquibase changelog table, but {master_label} does; "
                "set liquibase.schema in the config if it lives somewhere unexpected"
            )
        if self.status is ChangelogStatus.MISSING_IN_MASTER:
            return f"{master_label} has no Liquibase changelog table, but {target_label} does"
        if self.status is ChangelogStatus.AMBIGUOUS:
            return (
                "several changelog tables were found ("
                + ", ".join(self.candidates)
                + "); set liquibase.schema in the config to choose one"
            )

        if self.status is ChangelogStatus.FAILED_CHANGESETS:
            return self._failed_headline(master_label, target_label)
        if self.status is ChangelogStatus.LOCK_HELD:
            return "a deployment lock is held, so a migration may be in progress"
        if self.status is ChangelogStatus.HISTORY_DIFFERS:
            return (
                f"{target_label} and {master_label} have the same changesets, but their order, "
                "execution type, filename or checksum algorithm differ"
            )

        parts: list[str] = []
        if self.behind_count:
            first = self.missing_in_target[0]
            parts.append(
                f"{target_label} is {self.behind_count} changeset(s) behind {master_label} "
                f"(first missing: {first[0]} by {first[1]})"
            )
        if self.ahead_count:
            parts.append(f"{target_label} has {self.ahead_count} changeset(s) {master_label} lacks")
        if self.checksum_mismatches:
            parts.append(f"{len(self.checksum_mismatches)} checksum mismatch(es)")
        if not parts:
            parts.append(f"{target_label} matches {master_label}")
        return "; ".join(parts)

    def _failed_headline(self, master_label: str, target_label: str) -> str:
        """Say which side broke: an operator reading this goes and looks at that environment."""
        on_master = sum(1 for f in self.failed_changesets if f.side == "master")
        on_target = sum(1 for f in self.failed_changesets if f.side == "target")
        if on_master and on_target:
            return (
                f"{on_master} changeset(s) recorded as FAILED on {master_label} and "
                f"{on_target} on {target_label}; the migration did not complete on either"
            )
        if on_master:
            return (
                f"{on_master} changeset(s) recorded as FAILED on {master_label}; "
                "the migration did not complete there"
            )
        if on_target:
            return (
                f"{on_target} changeset(s) recorded as FAILED on {target_label}; "
                "the migration did not complete there"
            )
        return (
            f"{len(self.failed_changesets)} changeset(s) recorded as FAILED "
            "(side not recorded); the migration did not complete"
        )

    def tag_line(self, master_label: str, target_label: str) -> str | None:
        """``prod @ R7.6.2 · qa @ R7.6.1``.

        The cheapest and most human-readable line in the report: a release tag is what people
        actually recognise.
        """
        if self.master_tag is None and self.target_tag is None:
            return None
        return (
            f"{master_label} @ {self.master_tag or 'untagged'} · "
            f"{target_label} @ {self.target_tag or 'untagged'}"
        )

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "severity": self.severity.label,
            "master_count": self.master_count,
            "target_count": self.target_count,
            "behind_count": self.behind_count,
            "ahead_count": self.ahead_count,
            "missing_in_target": [list(r) for r in self.missing_in_target],
            "extra_in_target": [list(r) for r in self.extra_in_target],
            "checksum_mismatches": [m.to_json_dict() for m in self.checksum_mismatches],
            "checksum_algorithm_skew": self.checksum_algorithm_skew,
            "cleared_checksums": [list(r) for r in self.cleared_checksums],
            "exectype_differences": [d.to_json_dict() for d in self.exectype_differences],
            "filename_differences": [d.to_json_dict() for d in self.filename_differences],
            "order_inversions": [i.to_json_dict() for i in self.order_inversions],
            "failed_changesets": [list(f) for f in self.failed_changesets],
            "first_divergence": (list(self.first_divergence) if self.first_divergence else None),
            "master_tag": self.master_tag,
            "target_tag": self.target_tag,
            "master_location": self.master_location,
            "target_location": self.target_location,
            "candidates": list(self.candidates),
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class ChangelogOptions:
    """How strictly to compare."""

    strict: bool = False
    """Also compare the columns that ordinary changelog refactoring rewrites."""


def diff_changelog(
    master: ChangelogState | None,
    target: ChangelogState | None,
    *,
    options: ChangelogOptions | None = None,
) -> ChangelogDiff:
    """Compare two migration histories.

    An absent changelog is a reportable state, never an error: plenty of databases are not
    Liquibase-managed, and a comparison that crashed on one would be useless.
    """
    options = options or ChangelogOptions()
    absent = _absence_verdict(master, target)
    if absent is not None:
        return absent

    if master is None or target is None:  # pragma: no cover - implied by _absence_verdict
        raise RuntimeError("both changelogs must be present past the absence checks")

    master_rows = master.by_ref()
    target_rows = target.by_ref()

    missing = tuple(r.ref for r in master.rows if r.ref not in target_rows)
    extra = tuple(r.ref for r in target.rows if r.ref not in master_rows)
    shared = [r for r in master.rows if r.ref in target_rows]

    skew = _algorithm_skew(shared, target_rows)
    mismatches, cleared = _checksum_findings(shared, target_rows, skew=skew)
    exectypes = _exectype_findings(shared, target_rows)
    filenames = _filename_findings(shared, target_rows)
    inversions = _order_inversions(master.rows, target.rows, target_rows)
    failed = tuple(
        dict.fromkeys(
            FailedChangeset(r.id, r.author, side)
            for side, rows in (("master", master.rows), ("target", target.rows))
            for r in rows
            if r.exec_type.upper() == ExecType.FAILED.value
        )
    )

    notes: list[str] = []
    if skew:
        notes.append(
            "the two databases' checksums were produced by different Liquibase algorithm "
            "versions, so individual checksums are not comparable; only presence and execution "
            "type were compared"
        )
    if target.lock_held:
        notes.append(
            "a Liquibase deployment lock is held on the target, so a migration may have been in "
            "progress when this snapshot was taken"
        )
    if master.lock_held:
        notes.append("a Liquibase deployment lock is held on the master")
    if not options.strict:
        notes.append(
            "COMMENTS, CONTEXTS and LABELS were not compared; pass --strict-changelog to include "
            "them"
        )

    status = _status(missing, extra, mismatches)
    severity = _severity(
        status=status,
        mismatches=mismatches,
        exectypes=exectypes,
        filenames=filenames,
        inversions=inversions,
        failed=failed,
        cleared=cleared,
        skew=skew,
        lock_held=bool(target.lock_held),
    )

    # A verdict that carries a severity above INFO is never "in sync". Reporting it so would let the
    # severity be discarded by anything that only looks at the status.
    if status is ChangelogStatus.IN_SYNC and severity > Severity.INFO:
        if failed:
            status = ChangelogStatus.FAILED_CHANGESETS
        elif target.lock_held:
            status = ChangelogStatus.LOCK_HELD
        else:
            status = ChangelogStatus.HISTORY_DIFFERS

    return ChangelogDiff(
        status=status,
        severity=severity,
        master_count=master.count,
        target_count=target.count,
        missing_in_target=missing,
        extra_in_target=extra,
        checksum_mismatches=mismatches,
        checksum_algorithm_skew=skew,
        cleared_checksums=cleared,
        exectype_differences=exectypes,
        filename_differences=filenames,
        order_inversions=inversions,
        failed_changesets=failed,
        first_divergence=_first_divergence(master.rows, missing, mismatches, exectypes),
        master_tag=master.last_tag,
        target_tag=target.last_tag,
        master_location=master.location.qualified if master.location else None,
        target_location=target.location.qualified if target.location else None,
        notes=tuple(notes),
    )


def _absence_verdict(
    master: ChangelogState | None, target: ChangelogState | None
) -> ChangelogDiff | None:
    """Handle every way a changelog can be missing or ambiguous.

    Returns ``None`` when both are present and comparable.
    """
    master_present = master is not None and master.present
    target_present = target is not None and target.present

    if master is not None and master.ambiguous:
        return ChangelogDiff(
            status=ChangelogStatus.AMBIGUOUS,
            severity=Severity.WARNING,
            candidates=tuple(c.qualified for c in master.candidates),
        )
    if target is not None and target.ambiguous:
        return ChangelogDiff(
            status=ChangelogStatus.AMBIGUOUS,
            severity=Severity.WARNING,
            candidates=tuple(c.qualified for c in target.candidates),
        )

    if master_present and not target_present:
        # The master is Liquibase-managed and the target is not, which almost always means the
        # deployment never happened rather than that the target is managed differently.
        return ChangelogDiff(
            status=ChangelogStatus.MISSING_IN_TARGET,
            severity=Severity.ERROR,
            master_count=master.count if master else 0,
            master_location=master.location.qualified if master and master.location else None,
            master_tag=master.last_tag if master else None,
        )
    if target_present and not master_present:
        return ChangelogDiff(
            status=ChangelogStatus.MISSING_IN_MASTER,
            severity=Severity.WARNING,
            target_count=target.count if target else 0,
            target_location=target.location.qualified if target and target.location else None,
            target_tag=target.last_tag if target else None,
        )
    if not master_present and not target_present:
        # Neither is Liquibase-managed. Not a problem, just not applicable.
        return ChangelogDiff(status=ChangelogStatus.MISSING_IN_BOTH, severity=Severity.INFO)
    return None


def _algorithm_skew(
    shared: list[ChangeSetRow], target_rows: dict[ChangeSetRef, ChangeSetRow]
) -> bool:
    """Whether the two sides' checksums come from different Liquibase algorithm versions.

    Liquibase rewrites every checksum when it upgrades its algorithm, so without this a version
    upgrade in one environment reports every changeset in the database as edited.
    """
    for row in shared:
        other = target_rows[row.ref]
        left, right = row.checksum_algorithm, other.checksum_algorithm
        if left is not None and right is not None and left != right:
            return True
    return False


def _checksum_findings(
    shared: list[ChangeSetRow],
    target_rows: dict[ChangeSetRef, ChangeSetRow],
    *,
    skew: bool,
) -> tuple[tuple[ChecksumMismatch, ...], tuple[ChangeSetRef, ...]]:
    """Real content differences, and changesets whose checksum was cleared.

    Under algorithm skew no individual comparison is meaningful, so none is made.
    """
    mismatches: list[ChecksumMismatch] = []
    cleared: list[ChangeSetRef] = []

    for row in shared:
        other = target_rows[row.ref]
        if row.md5sum is None or other.md5sum is None:
            if row.md5sum != other.md5sum:
                # clearCheckSums was run on one side, so nothing can be verified about content.
                cleared.append(row.ref)
            continue
        if skew:
            continue
        if row.checksum_digest != other.checksum_digest:
            mismatches.append(ChecksumMismatch(row.ref, row.md5sum, other.md5sum))

    return tuple(mismatches), tuple(cleared)


def _exectype_findings(
    shared: list[ChangeSetRow], target_rows: dict[ChangeSetRef, ChangeSetRow]
) -> tuple[ExecTypeDifference, ...]:
    """Changesets recorded with a different execution type on each side."""
    out: list[ExecTypeDifference] = []
    for row in shared:
        other = target_rows[row.ref]
        left, right = row.exec_type.upper(), other.exec_type.upper()
        if left == right:
            continue
        out.append(ExecTypeDifference(row.ref, left, right, _exectype_severity(left, right)))
    return tuple(out)


def _exectype_severity(left: str, right: str) -> Severity:
    """How much an execution-type difference matters.

    ``EXECUTED`` against ``MARK_RAN`` is expected in this platform — a precondition evaluated
    differently, usually because the *data* differs, which ``acme-invoicing`` does deliberately.
    Worth reading, not worth failing a build over.
    """
    if ExecType.FAILED.value in (left, right):
        return Severity.ERROR
    pair = {left, right}
    if pair == {ExecType.EXECUTED.value, ExecType.MARK_RAN.value}:
        return Severity.WARNING
    if pair <= {ExecType.EXECUTED.value, ExecType.RERAN.value, ExecType.SKIPPED.value}:
        return Severity.INFO
    return Severity.WARNING


def _filename_findings(
    shared: list[ChangeSetRow], target_rows: dict[ChangeSetRef, ChangeSetRow]
) -> tuple[FilenameDifference, ...]:
    """Filename differences, graded by whether the file itself differs.

    A different *path* to the same file is how ``relativeToChangelogFile`` and ``logicalFilePath``
    present themselves, and says nothing. A different *basename* means a changeset moved between
    files, which is worth a look.
    """
    out: list[FilenameDifference] = []
    for row in shared:
        other = target_rows[row.ref]
        if row.filename == other.filename:
            continue
        severity = Severity.INFO if row.basename == other.basename else Severity.WARNING
        out.append(FilenameDifference(row.ref, row.filename, other.filename, severity))
    return tuple(out)


def _order_inversions(
    master_rows: tuple[ChangeSetRow, ...],
    target_rows_ordered: tuple[ChangeSetRow, ...],
    target_rows: dict[ChangeSetRef, ChangeSetRow],
) -> tuple[OrderInversion, ...]:
    """Pairs applied in opposite relative order on the two sides.

    ``ORDEREXECUTED`` itself is a per-deployment counter and is never compared numerically: the same
    changeset is row 7 in one environment and row 12 in another purely because of deployment
    history. Only the *relative* order of the changesets present in both is meaningful, so this
    compares the two sequences and reports the pairs that a longest-common-subsequence match cannot
    reconcile.
    """
    master_sequence = [r.ref for r in master_rows if r.ref in target_rows]
    target_sequence = [r.ref for r in target_rows_ordered if r.ref in target_rows]
    target_sequence = [ref for ref in target_sequence if ref in set(master_sequence)]

    if master_sequence == target_sequence or not master_sequence:
        return ()

    position = {ref: index for index, ref in enumerate(target_sequence)}
    matcher = SequenceMatcher(a=master_sequence, b=target_sequence, autojunk=False)
    common = {
        master_sequence[i]
        for block in matcher.get_matching_blocks()
        for i in range(block.a, block.a + block.size)
    }

    out: list[OrderInversion] = []
    moved = [ref for ref in master_sequence if ref not in common]
    for ref in moved:
        for anchor in master_sequence:
            if anchor in moved or anchor not in position or ref not in position:
                continue
            master_first = master_sequence.index(ref) < master_sequence.index(anchor)
            target_first = position[ref] < position[anchor]
            if master_first != target_first:
                pair = (ref, anchor) if master_first else (anchor, ref)
                out.append(OrderInversion(*pair))
                break
    return tuple(out)


def _status(
    missing: tuple[ChangeSetRef, ...],
    extra: tuple[ChangeSetRef, ...],
    mismatches: tuple[ChecksumMismatch, ...],
) -> ChangelogStatus:
    """The headline verdict.

    Checksum mismatches outrank counts: a changeset that was *edited after deployment* is a worse
    problem than one that has not been deployed yet, because nothing will ever re-apply it.
    """
    if mismatches:
        return ChangelogStatus.CHECKSUM_MISMATCH
    if missing and extra:
        return ChangelogStatus.DIVERGED
    if missing:
        return ChangelogStatus.TARGET_BEHIND
    if extra:
        return ChangelogStatus.TARGET_AHEAD
    return ChangelogStatus.IN_SYNC


def _severity(
    *,
    status: ChangelogStatus,
    mismatches: tuple[ChecksumMismatch, ...],
    exectypes: tuple[ExecTypeDifference, ...],
    filenames: tuple[FilenameDifference, ...],
    inversions: tuple[OrderInversion, ...],
    failed: tuple[FailedChangeset, ...],
    cleared: tuple[ChangeSetRef, ...],
    skew: bool,
    lock_held: bool,
) -> Severity:
    """The worst thing found."""
    if failed or mismatches or status is ChangelogStatus.TARGET_BEHIND:
        return Severity.ERROR
    if status is ChangelogStatus.DIVERGED:
        return Severity.ERROR
    candidates: list[Severity] = [d.severity for d in exectypes]
    candidates.extend(d.severity for d in filenames)
    if inversions or skew or cleared or lock_held or status is ChangelogStatus.TARGET_AHEAD:
        candidates.append(Severity.WARNING)
    return max(candidates) if candidates else Severity.INFO


def _first_divergence(
    master_rows: tuple[ChangeSetRow, ...],
    missing: tuple[ChangeSetRef, ...],
    mismatches: tuple[ChecksumMismatch, ...],
    exectypes: tuple[ExecTypeDifference, ...],
) -> ChangeSetRef | None:
    """The earliest master changeset that went wrong.

    Walked in the master's own applied order, so the answer is "the histories split here" rather
    than a list of every consequence downstream of the split.
    """
    suspect = {*missing, *(m.ref for m in mismatches), *(d.ref for d in exectypes)}
    if not suspect:
        return None
    for row in master_rows:
        if row.ref in suspect:
            return row.ref
    return None
