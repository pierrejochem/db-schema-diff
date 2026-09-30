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
* A ``Session`` runs one comparison or ``check_all`` at a time; starting another while one is
  active (including one still draining after a cancel) raises ``GuiError``.

Cancellation
------------
Python cannot interrupt a thread inside a database call, and the library exposes no hook to abort
one. ``cancel()`` therefore does the strongest thing available:

* sources not yet started never start (a flag is checked before each capture, and a source waits on
  a semaphore before it is handed to a thread);
* sources in flight run until they finish and their result is discarded. The only bound is the
  library's timeouts, and those are per statement: one capture issues 14 catalog queries plus the
  changelog queries, so a discarded capture can keep querying for roughly
  ``connect_timeout + 14 x statement_timeout`` - about 14 minutes at the defaults (10 s and 60 s),
  and far longer if ``statement_timeout`` is raised (the maximum is 3600 s). The worst case is
  minutes, not seconds, and the window must not imply otherwise;
* every source that has not already finished is reported ``CANCELLED`` immediately, so the window
  updates at once - well before production has necessarily gone quiet;
* ``compare`` then waits for the in-flight threads to end, however long that takes, before it raises
  ``CancelledError``, and nothing delivered during that wait (a second ``cancel()``, a task
  cancellation) cuts it short. The session stays busy until then, so a new run can never overlap an
  old capture. The wait is on the drain, not on the caller: the UI is already told.

No partial report is ever returned. A cancelled session can be used again.

No credential appears in an event, a log line or an exception message: credentials travel only as
``Dsn``, and anything shown comes from the library's already-redacted messages, scrubbed again here
as a second layer, or is a fixed string naming the exception type.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import TypeVar

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

_R = TypeVar("_R")


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
        """Check the master and every target, in configuration order.

        Bounded by ``max_workers`` (one at a time when ``parallel`` is off) and stoppable with
        ``cancel()``, exactly like ``compare``; cancelling raises ``CancelledError``.
        """
        sources = [self._config.master, *self._config.targets]
        options = self._config.options
        workers = min(len(sources), options.max_workers) if options.parallel else 1
        self._begin("a check or comparison is already running")
        try:
            done = await self._drive(
                sources,
                workers,
                self._check_blocking,
                lambda source, exc: ConnectionStatus(
                    label=source.label,
                    ok=False,
                    error=f"{source.label}: check failed unexpectedly ({type(exc).__name__})",
                ),
                lambda status: (
                    SourceState.CAPTURED if status.ok else SourceState.FAILED,
                    None,
                ),
                announce=False,
            )
            if any(source.label not in done for source in sources):
                raise asyncio.CancelledError
            return [done[source.label] for source in sources]
        finally:
            self._end()

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
        self._begin("a check or comparison is already running")
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
            self._end()

    def _begin(self, busy_message: str) -> None:
        if self._running:
            raise GuiError(busy_message)
        self._running = True
        self._generation += 1
        self._cancel_requested.clear()
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()

    def _end(self) -> None:
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
        finished = await self._drive(
            sources,
            workers,
            lambda source: self._capture_blocking(source, skip_liquibase),
            lambda source, exc: CaptureResult(
                label=source.label,
                error=f"{source.label}: capture failed unexpectedly ({type(exc).__name__})",
            ),
            lambda result: (
                (SourceState.CAPTURED, None) if result.ok else (SourceState.FAILED, result.error)
            ),
            announce=True,
        )
        if any(source.label not in finished for source in sources):
            # A source that settled as cancelled while the gather completed: never build a report
            # from what is left.
            raise asyncio.CancelledError
        return {source.label: finished[source.label] for source in sources}

    async def _drive(
        self,
        sources: list[SourceRef],
        workers: int,
        work: Callable[[SourceRef], _R | None],
        failure: Callable[[SourceRef, BaseException], _R],
        outcome: Callable[[_R], tuple[SourceState, str | None]],
        *,
        announce: bool,
    ) -> dict[str, _R]:
        """Run ``work`` for every source on at most ``workers`` threads, cancellably.

        ``work`` returns ``None`` if it was cancelled before it started. Returns the results of
        the sources that finished; raises ``CancelledError`` after the threads have ended.
        """
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="capture")
        slots = asyncio.Semaphore(workers)
        finished: dict[str, _R] = {}
        terminal: set[str] = set()
        loop = asyncio.get_running_loop()

        def settle(label: str, state: SourceState, detail: str | None = None) -> None:
            """Record a source's one terminal state."""
            if label in terminal:
                return
            terminal.add(label)
            if announce:
                self._emit(ProgressEvent(label, state, detail))

        async def one(source: SourceRef) -> None:
            result: _R | None
            async with slots:
                if self._cancel_requested.is_set():
                    settle(source.label, SourceState.CANCELLED)
                    return
                if announce:
                    self._emit(ProgressEvent(source.label, SourceState.CAPTURING))
                try:
                    result = await loop.run_in_executor(pool, work, source)
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    log.warning("%s: failed unexpectedly (%s)", source.label, type(exc).__name__)
                    result = failure(source, exc)
            if result is None or source.label in terminal:
                settle(source.label, SourceState.CANCELLED)
                return
            finished[source.label] = result
            state, detail = outcome(result)
            settle(source.label, state, detail)

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
            # waited for. The wait cannot be cut short: a cancellation delivered meanwhile is
            # absorbed, because abandoning it would free the session while a capture still queries.
            drain = asyncio.ensure_future(
                asyncio.to_thread(pool.shutdown, True, cancel_futures=True)
            )
            while not drain.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(drain)
        return finished

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
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            # Whatever the consumer does, the run must still settle every source.
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
    """Second layer, as strong as the library's, over a message it already redacted.

    Every fragment is replaced whatever its length, and any token containing ``@`` goes too, since
    a password fragment can travel inside a mis-parsed host.
    """
    cleaned = text.replace(dsn.value, "***")
    for fragment in dsn.secret_fragments():
        cleaned = cleaned.replace(fragment, "***")
    return re.sub(r"\S*@\S*", "***", cleaned)


__all__ = ["ProgressEvent", "Session", "SourceState"]
