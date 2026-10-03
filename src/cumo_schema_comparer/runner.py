"""Orchestration: capture every source, diff each target, assemble one report.

The only module that does IO *and* diffing *and* reporting. Everything it coordinates is pure
or self-contained, so this stays a readable sequence rather than a place logic accumulates.

Two decisions shape it:

**Each source is captured independently.** One unreachable target must not abort the others: a
report covering three of four environments is useful, and a run that dies on the first refused
connection is not. A failed source becomes a skipped entry, and the exit code says the
comparison was incomplete.

**Capture is parallel by default**, via threads. Introspection is IO-bound, psycopg releases the
GIL while waiting on the network, and the resulting inventories have to be shared in one
process — so asyncio buys nothing here and multiprocessing would cost pickling.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from . import __version__
from .build import build_inventory
from .config.model import ComparerConfig, SourceRef
from .config.secrets import Dsn, Secret
from .db.connect import ConnectionOptions, open_connection, server_features
from .db.introspect import Introspector
from .diff.changelog import ChangelogOptions
from .diff.engine import diff_inventories
from .diff.ignores import IgnoreRuleSet
from .diff.model import ComparisonReport, DiffOptions, Note, NoteKind, TargetDiff
from .diff.severity import Severity
from .errors import ComparerError
from .model.changelog import ChangelogLocation, ChangelogState
from .model.inventory import Inventory

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CaptureResult:
    """One source's capture: an inventory, or why there isn't one."""

    label: str
    inventory: Inventory | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.inventory is not None


def capture(
    source: SourceRef,
    dsn: Dsn,
    *,
    ssh_passphrase: Secret | None = None,
    exclude_schemas: tuple[str, ...] = (),
    options: ConnectionOptions | None = None,
    skip_liquibase: bool = False,
    redact_literals: bool = True,
) -> CaptureResult:
    """Inventory one source, converting any expected failure into a result value.

    Returning the failure rather than raising is what lets the other sources finish.
    """
    try:
        with open_connection(
            dsn,
            label=source.label,
            options=options,
            version=__version__,
            ssh=source.ssh,
            ssh_passphrase=ssh_passphrase,
        ) as connection:
            inventory = build_inventory(
                connection,
                source,
                exclude_schemas=exclude_schemas,
                skip_liquibase=skip_liquibase,
                redact_literals=redact_literals,
            )
    except ComparerError as exc:
        log.warning("%s: capture failed", source.label)
        return CaptureResult(label=source.label, error=str(exc))
    return CaptureResult(label=source.label, inventory=inventory)


def connection_options(config: ComparerConfig) -> ConnectionOptions:
    """Timeouts for this config, shared by every capture."""
    return ConnectionOptions(
        connect_timeout=config.options.connect_timeout_seconds,
        statement_timeout=config.options.statement_timeout_seconds,
        lock_timeout=config.options.lock_timeout_seconds,
    )


@dataclass(frozen=True, slots=True)
class ConnectionStatus:
    """What one source looks like, without capturing its inventory.

    Answers the questions that otherwise turn into a confusing comparison later: which server
    version, which schemas, and where the Liquibase changelog actually lives.
    """

    label: str
    ok: bool
    server_version: str | None = None
    database: str | None = None
    user: str | None = None
    encoding: str | None = None
    collation: str | None = None
    schemas: tuple[str, ...] = ()
    changelog: str | None = None
    changelog_count: int | None = None
    changelog_tag: str | None = None
    changelog_candidates: tuple[str, ...] = ()
    changelog_locked: bool | None = None
    error: str | None = None
    """Already redacted. Never contains a connection string."""


def check_connection(
    source: SourceRef,
    dsn: Dsn,
    *,
    ssh_passphrase: Secret | None = None,
    options: ConnectionOptions | None = None,
    exclude_schemas: tuple[str, ...] = (),
) -> ConnectionStatus:
    """Connect to one source and report what is there, without inventorying it.

    Uses the same hardened read-only connection a comparison uses, with the same timeouts, so a
    successful check means the comparison will connect too — not merely that a socket opened.

    A failure is returned rather than raised: callers show one row per source, and one unreachable
    host must not stop the others.
    """
    try:
        with open_connection(
            dsn,
            label=source.label,
            options=options,
            version=__version__,
            ssh=source.ssh,
            ssh_passphrase=ssh_passphrase,
        ) as conn:
            introspector = Introspector(conn, server_features(conn))
            info = introspector.server_info()
            schemas = introspector.schemas(exclude=exclude_schemas, only=source.schemas)
            changelog = _changelog_summary(introspector, source)
    except ComparerError as exc:
        return ConnectionStatus(label=source.label, ok=False, error=str(exc))

    return ConnectionStatus(
        label=source.label,
        ok=True,
        server_version=str(info["server_version"]),
        database=str(info["database"]),
        user=str(info["user"]),
        encoding=str(info["encoding"]),
        collation=str(info["datcollate"]),
        schemas=tuple(schemas),
        **changelog,
    )


def _changelog_summary(introspector: Introspector, source: SourceRef) -> dict[str, Any]:
    """Where the changelog is, or why that cannot be answered.

    Located rather than assumed: it is not reliably in ``public``.
    """
    if source.liquibase is not None:
        location = ChangelogLocation(
            schema=source.liquibase.schema_name, table=source.liquibase.table
        )
        state = introspector.changelog_state(location)
        return _changelog_fields(state)

    candidates = introspector.locate_changelog()
    if not candidates:
        return {}
    if len(candidates) > 1:
        return {"changelog_candidates": tuple(c.qualified for c in candidates)}
    return _changelog_fields(introspector.changelog_state(candidates[0]))


def _changelog_fields(state: ChangelogState) -> dict[str, Any]:
    if state.location is None:
        return {}
    return {
        "changelog": state.location.qualified,
        "changelog_count": state.count,
        "changelog_tag": state.last_tag,
        "changelog_locked": state.lock_held,
    }


def capture_all(
    config: ComparerConfig,
    credentials: dict[str, Dsn],
    *,
    ssh_passphrases: dict[str, Secret] | None = None,
    targets: tuple[str, ...] | None = None,
    sequential: bool = False,
    skip_liquibase: bool = False,
    redact_literals: bool = True,
) -> dict[str, CaptureResult]:
    """Capture the master and the selected targets.

    ``targets`` selects a subset by label; ``None`` means all of them.
    """
    selected = _selected_sources(config, targets)
    options = connection_options(config)

    passphrases = ssh_passphrases or {}

    def run(source: SourceRef) -> CaptureResult:
        return capture(
            source,
            credentials[source.label],
            ssh_passphrase=passphrases.get(source.label),
            exclude_schemas=config.exclude_schemas,
            options=options,
            skip_liquibase=skip_liquibase,
            redact_literals=redact_literals,
        )

    if sequential or not config.options.parallel or len(selected) == 1:
        return {source.label: run(source) for source in selected}

    workers = min(len(selected), config.options.max_workers)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="capture") as pool:
        results = list(pool.map(run, selected))
    return {result.label: result for result in results}


def compare(
    config: ComparerConfig,
    credentials: dict[str, Dsn],
    *,
    ssh_passphrases: dict[str, Secret] | None = None,
    targets: tuple[str, ...] | None = None,
    sequential: bool = False,
    diff_options: DiffOptions | None = None,
    ignores: IgnoreRuleSet | None = None,
    changelog_options: ChangelogOptions | None = None,
    skip_liquibase: bool = False,
    redact_literals: bool = True,
) -> ComparisonReport:
    """Capture every source and compare each target against the master."""
    captures = capture_all(
        config,
        credentials,
        ssh_passphrases=ssh_passphrases,
        targets=targets,
        sequential=sequential,
        skip_liquibase=skip_liquibase,
        redact_literals=redact_literals,
    )
    return build_report(
        config,
        captures,
        targets=targets,
        diff_options=diff_options,
        ignores=ignores,
        changelog_options=changelog_options,
    )


def build_report(
    config: ComparerConfig,
    captures: dict[str, CaptureResult],
    *,
    targets: tuple[str, ...] | None = None,
    diff_options: DiffOptions | None = None,
    ignores: IgnoreRuleSet | None = None,
    changelog_options: ChangelogOptions | None = None,
) -> ComparisonReport:
    """Assemble a report from already-captured inventories.

    Separate from :func:`compare` so the same assembly is used by ``diff-inventories``, and so
    it can be tested without a database.
    """
    notes: list[Note] = [
        Note(kind=NoteKind.PROBE_FAILED, message=warning, severity=Severity.WARNING)
        for warning in config.warnings()
    ]

    master_capture = captures[config.master.label]
    selected_targets = _selected_targets(config, targets)

    if not master_capture.ok:
        # Without the reference there is nothing to compare against, so every target is
        # reported as skipped rather than silently omitted.
        notes.append(
            Note(
                kind=NoteKind.PROBE_FAILED,
                message=f"master {config.master.label!r} could not be inspected: "
                f"{master_capture.error}",
                severity=Severity.ERROR,
            )
        )
        skipped = tuple(
            TargetDiff(
                master_label=config.master.label,
                target_label=target.label,
                master_source=None,
                target_source=None,
                failed="master could not be inspected",
            )
            for target in selected_targets
        )
        return _report(config, skipped, notes, probe_failed=True)

    master = master_capture.inventory
    if master is None:  # pragma: no cover - implied by master_capture.ok
        raise RuntimeError("master capture reported success without an inventory")

    diffs: list[TargetDiff] = []
    probe_failed = False
    for target in selected_targets:
        result = captures[target.label]
        if not result.ok:
            probe_failed = True
            diffs.append(
                TargetDiff(
                    master_label=config.master.label,
                    target_label=target.label,
                    master_source=master.source,
                    target_source=None,
                    failed=result.error,
                )
            )
            continue
        target_inventory = result.inventory
        if target_inventory is None:  # pragma: no cover - implied by result.ok
            raise RuntimeError(f"{target.label}: capture reported success without an inventory")
        diffs.append(
            diff_inventories(
                master,
                target_inventory,
                options=diff_options or _diff_options(config),
                schema_map=_inverse_schema_map(target),
                ignores=ignores,
                changelog_options=changelog_options,
            )
        )

    return _report(config, tuple(diffs), notes, probe_failed=probe_failed)


def _report(
    config: ComparerConfig,
    diffs: tuple[TargetDiff, ...],
    notes: list[Note],
    *,
    probe_failed: bool,
) -> ComparisonReport:
    return ComparisonReport(
        name=config.name,
        master_label=config.master.label,
        targets=diffs,
        notes=tuple(notes),
        generated_at=datetime.now(UTC).isoformat(),
        tool_version=__version__,
        fail_on=config.options.fail_on,
        probe_failed=probe_failed,
    )


def _diff_options(config: ComparerConfig) -> DiffOptions:
    return DiffOptions(
        ignore_column_order=config.options.ignore_column_order,
        include_owners=config.options.include_owners,
        include_comments=config.options.include_comments,
        include_grants=config.options.include_grants,
    )


def _inverse_schema_map(target: SourceRef) -> dict[str, str]:
    """Target schema name to master schema name.

    The config is written the way a person thinks — master name first — and comparison happens
    in the master's namespace, so the map is inverted here.
    """
    return {target_name: master_name for master_name, target_name in target.schema_map.items()}


def _selected_sources(config: ComparerConfig, targets: tuple[str, ...] | None) -> list[SourceRef]:
    return [config.master, *_selected_targets(config, targets)]


def _selected_targets(config: ComparerConfig, targets: tuple[str, ...] | None) -> list[SourceRef]:
    if targets is None:
        return list(config.targets)
    return [config.target(label) for label in targets]
