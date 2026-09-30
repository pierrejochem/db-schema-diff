"""Orchestration for the application.

The Slint event loop is an asyncio loop, so a coroutine here runs on the same thread as the UI and
can write properties directly. Every blocking call (database, keychain, diffing) runs on a worker
thread. That is the single concurrency invariant, and it is what keeps the window responsive.

Captures are driven per source rather than through ``runner.capture_all``, which is what makes
per-source progress possible without changing the library.

Threading contract
------------------
* ``on_progress`` is only ever invoked on the event-loop thread, never from a worker. Workers do
  the blocking call and return a value; the coroutine that awaited them reports it. The consumer
  therefore does not need to be thread-safe and may touch Slint properties directly.
* ``on_progress`` runs inline in the session: keep it quick. If it raises, the exception is logged
  (type only) and dropped; a broken consumer cannot fail a comparison.
* ``cancel()`` may be called from any thread.
* A ``Session`` runs one comparison at a time; a second ``compare`` while one is active (including
  one that is still draining after a cancel) raises ``GuiError``.

Cancellation
------------
Python cannot interrupt a thread inside a database call, and the library exposes no hook to abort
one. ``cancel()`` therefore does the strongest thing available:

* sources not yet started never start (a flag is checked before each capture, and a source waits on
  a semaphore before it is handed to a thread);
* sources in flight run until they finish, bounded by the configured statement and connect
  timeouts, and their result is discarded;
* every source that has not already finished is reported ``CANCELLED`` immediately, so the window
  updates at once;
* ``compare`` then waits for the in-flight threads to end before it raises ``CancelledError``, so
  when it has raised nothing is still touching a database and a new run cannot overlap the old one.

No partial report is ever returned. A cancelled session can be used again.

No credential appears in an event, a log line or an exception message: credentials travel only as
``Dsn``, and anything shown comes from the library's already-redacted messages, scrubbed again here
as a second layer, or is a fixed string naming the exception type.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from .. import runner
from ..baseline import apply_baseline
from ..config.model import ComparerConfig, SourceRef
from ..config.secrets import Dsn
from ..diff.changelog import ChangelogOptions
from ..diff.ignores import IgnoreRuleSet
from ..diff.model import ComparisonReport
from ..errors import MissingCredentialsError
from ..report.json_report import load_report
from ..runner import CaptureResult, ConnectionStatus
from .credentials import CredentialStore
from .errors import GuiError

log = logging.getLogger(__name__)

#: Shorter fragments would mangle ordinary words when scrubbed out of a message.
_MIN_SCRUB_LENGTH = 6


class SourceState(StrEnum):
    """Where one source is in the current run."""

    IDLE = "idle"
    CONNECTING = "connecting"
    CAPTURING = "capturing"
    CAPTURED = "captured"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL = frozenset({SourceState.CAPTURED, SourceState.FAILED, SourceState.CANCELLED})


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One source's state changed. Always delivered on the event-loop thread."""

    label: str
    state: SourceState
    detail: str | None = None
    """Already redacted. Never contains a connection string."""


class Session:
    """One configuration's checks and comparisons."""

    def __init__(
        self,
        config: ComparerConfig,
        credentials: CredentialStore,
        *,
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ) -> None:
        self._config = config
        self._credentials = credentials
        self._on_progress = on_progress
        self._cancel_requested = threading.Event()
        self._running = False
        self._generation = 0
        self._task: asyncio.Task[object] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- connection checks -------------------------------------------------------------------

    async def check_connection(self, label: str) -> ConnectionStatus:
        """Check one source. A failure is returned; an unknown label raises ``GuiError``."""
        source = self._source(label)
        return await asyncio.to_thread(self._check_blocking, source)

    async def check_all(self) -> list[ConnectionStatus]:
        """Check the master and every target, concurrently, in configuration order."""
        sources = [self._config.master, *self._config.targets]
        return list(
            await asyncio.gather(*(asyncio.to_thread(self._check_blocking, s) for s in sources))
        )

    def _source(self, label: str) -> SourceRef:
        for source in (self._config.master, *self._config.targets):
            if source.label == label:
                return source
        raise GuiError(f"no source labelled {label!r} in this configuration")

    def _check_blocking(self, source: SourceRef) -> ConnectionStatus:
        try:
            dsn = self._credentials.resolve(source.dsn_env)
        except MissingCredentialsError as exc:
            return ConnectionStatus(label=source.label, ok=False, error=str(exc))
        except Exception as exc:
            log.warning("%s: credential lookup failed (%s)", source.label, type(exc).__name__)
            return ConnectionStatus(
                label=source.label,
                ok=False,
                error=f"{source.label}: credential lookup failed ({type(exc).__name__})",
            )
        try:
            status = runner.check_connection(
                source,
                dsn,
                options=runner.connection_options(self._config),
                exclude_schemas=self._config.exclude_schemas,
            )
        except Exception as exc:
            log.warning("%s: check failed (%s)", source.label, type(exc).__name__)
            return ConnectionStatus(
                label=source.label,
                ok=False,
                error=f"{source.label}: check failed unexpectedly ({type(exc).__name__})",
            )
        if status.error is None:
            return status
        return replace(status, error=_scrub(status.error, dsn))

    # -- comparison --------------------------------------------------------------------------

    async def compare(
        self,
        *,
        ignores: IgnoreRuleSet | None = None,
        changelog_options: ChangelogOptions | None = None,
        skip_liquibase: bool = False,
        sequential: bool = False,
        baseline: ComparisonReport | None = None,
    ) -> ComparisonReport:
        """Capture every source and build the report.

        Raises ``asyncio.CancelledError`` if cancelled; never returns a partial report. One
        source failing does not stop the others: it becomes a skipped target in the report.
        """
        if self._running:
            raise GuiError("a comparison is already running")
        self._running = True
        self._generation += 1
        self._cancel_requested.clear()
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        try:
            captures = await self._capture_sources(sequential, skip_liquibase)
            report = await asyncio.to_thread(
                runner.build_report,
                self._config,
                captures,
                ignores=ignores,
                changelog_options=changelog_options,
            )
            if baseline is not None:
                report = apply_baseline(report, baseline).report
            return report
        finally:
            self._task = None
            self._running = False

    async def _capture_sources(
        self, sequential: bool, skip_liquibase: bool
    ) -> dict[str, CaptureResult]:
        sources = [self._config.master, *self._config.targets]
        options = self._config.options
        workers = (
            1 if sequential or not options.parallel else min(len(sources), options.max_workers)
        )
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="capture")
        slots = asyncio.Semaphore(workers)
        finished: dict[str, CaptureResult] = {}
        terminal: set[str] = set()
        loop = asyncio.get_running_loop()

        def settle(label: str, state: SourceState, detail: str | None = None) -> bool:
            """Record a source's one terminal state. False if it already has one."""
            if label in terminal:
                return False
            terminal.add(label)
            self._emit(ProgressEvent(label, state, detail))
            return True

        async def one(source: SourceRef) -> None:
            async with slots:
                if self._cancel_requested.is_set():
                    settle(source.label, SourceState.CANCELLED)
                    return
                self._emit(ProgressEvent(source.label, SourceState.CAPTURING))
                try:
                    result = await loop.run_in_executor(
                        pool, self._capture_blocking, source, skip_liquibase
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # pragma: no cover - _capture_blocking never raises
                    result = CaptureResult(
                        label=source.label, error=f"{source.label}: {type(exc).__name__}"
                    )
            if result is None or source.label in terminal:
                settle(source.label, SourceState.CANCELLED)
                return
            if result.ok:
                finished[source.label] = result
                settle(source.label, SourceState.CAPTURED)
            else:
                finished[source.label] = result
                settle(source.label, SourceState.FAILED, result.error)

        tasks = [asyncio.create_task(one(source)) for source in sources]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            self._cancel_requested.set()
            for task in tasks:
                task.cancel()
            # Report at once, before waiting for in-flight threads, so the window updates now.
            for source in sources:
                settle(source.label, SourceState.CANCELLED)
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            # Nothing is left running when this returns: queued work is dropped, in-flight work is
            # waited for. On the normal path every thread has already finished, so this is instant.
            await asyncio.shield(asyncio.to_thread(pool.shutdown, True, cancel_futures=True))
        return {source.label: finished[source.label] for source in sources}

    def _capture_blocking(self, source: SourceRef, skip_liquibase: bool) -> CaptureResult | None:
        """Runs on a worker thread. Returns ``None`` if cancelled before it started.

        Never raises, and never touches ``on_progress``.
        """
        if self._cancel_requested.is_set():
            return None
        try:
            dsn = self._credentials.resolve(source.dsn_env)
        except MissingCredentialsError as exc:
            return CaptureResult(label=source.label, error=str(exc))
        except Exception as exc:
            log.warning("%s: credential lookup failed (%s)", source.label, type(exc).__name__)
            return CaptureResult(
                label=source.label,
                error=f"{source.label}: credential lookup failed ({type(exc).__name__})",
            )
        try:
            result = runner.capture(
                source,
                dsn,
                exclude_schemas=self._config.exclude_schemas,
                options=runner.connection_options(self._config),
                skip_liquibase=skip_liquibase,
            )
        except Exception as exc:
            # Only the type: an unexpected exception's text is not under the library's redaction.
            log.warning("%s: capture failed unexpectedly (%s)", source.label, type(exc).__name__)
            error = f"{source.label}: capture failed unexpectedly ({type(exc).__name__})"
            return CaptureResult(label=source.label, error=error)
        if result.error is not None:
            return CaptureResult(label=result.label, error=_scrub(result.error, dsn))
        return result

    def _emit(self, event: ProgressEvent) -> None:
        if self._on_progress is None:
            return
        try:
            self._on_progress(event)
        except Exception as exc:
            log.warning("progress consumer raised (%s)", type(exc).__name__)

    # -- baseline and cancellation -----------------------------------------------------------

    @staticmethod
    def load_baseline(path: Path) -> ComparisonReport:
        """Read an approved JSON report for use as a baseline."""
        try:
            return load_report(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # ``json.JSONDecodeError`` is a ``ValueError``; its text quotes the document, so only
            # the exception type is repeated.
            raise GuiError(
                f"{path}: not a readable baseline report ({type(exc).__name__}). "
                "Choose a JSON report produced by this tool."
            ) from None

    def cancel(self) -> None:
        """Stop the current comparison. Safe from any thread; a no-op when nothing is running.

        See the module docstring for exactly what happens to in-flight work.
        """
        self._cancel_requested.set()
        loop, generation = self._loop, self._generation
        if not self._running or loop is None:
            return
        with contextlib.suppress(RuntimeError):  # the loop is already closed
            loop.call_soon_threadsafe(self._cancel_task, generation)

    def _cancel_task(self, generation: int) -> None:
        # The generation stops a late-arriving cancel from killing a later run.
        task = self._task
        if generation == self._generation and task is not None and not task.done():
            task.cancel()


def _scrub(text: str, dsn: Dsn) -> str:
    """Second layer: remove any credential that reached a message the library already redacted."""
    cleaned = text.replace(dsn.value, "***")
    for fragment in dsn.secret_fragments():
        if len(fragment) >= _MIN_SCRUB_LENGTH:
            cleaned = cleaned.replace(fragment, "***")
    return cleaned


__all__ = ["ProgressEvent", "Session", "SourceState"]
