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
  library's timeouts, and those are per statement: one capture issues 20 queries (measured: 26
  statements, 6 of them session ``SET``s), so a discarded capture can keep querying for roughly
  ``connect_timeout + 20 x statement_timeout`` - about 20 minutes at the defaults (10 s and 60 s),
  and about 20 hours at the maximum ``statement_timeout`` of 3600 s. The worst case is minutes or
  more, not seconds, and the window must not imply otherwise;
* every source that has not already finished is reported ``CANCELLED`` immediately, so the window
  updates at once - well before production has necessarily gone quiet;
* the run then waits for the in-flight threads to end, however long that takes, and raises
  ``CancelledError``. That wait cannot be shortened: a second ``cancel()`` or a task cancellation
  delivered during it is absorbed (it is not lost - the run still ends in ``CancelledError``, never
  with results, because the cancel flag is checked after the wait). The session stays busy until
  then, so a new run can never overlap an old capture. The wait is on the drain, not on the
  caller: the UI is already told.

Releasing an abandoned run
--------------------------
If the coroutine is created and then dropped without being awaited, or its task is cancelled before
its first turn, the session must not stay "running". Two mechanisms cover it, and they are not
equal:

* **The guarantee** is the check made by the *next* ``compare``/``check_all``/``check_connection``
  call: a claim whose coroutine is gone (weak reference dead) or closed without ever having run is
  released there. It depends on nothing but the coroutine having been freed, which reference
  counting does at once on CPython.
* **An optimisation** is a ``weakref.finalize`` on the coroutine that releases the claim as soon as
  it is freed, so ``cancel()`` and busy checks see the truth without waiting for a next call. Do not
  rely on it: it is garbage-collection-timing dependent. A coroutine that is still referenced
  somewhere (a debugger, a cycle not yet collected) is indistinguishable from one about to be
  awaited, and keeps the session busy until it is freed.

Python warns ``coroutine ... was never awaited`` for such a dropped coroutine. That is left visible
on purpose: it reports a real caller bug.

Reading a cancelled run
-----------------------
Per-source events are truthful history, not a verdict. A run that is cancelled late can have emitted
``CAPTURED`` for every source and still end in ``CancelledError`` (a cancel that arrived while the
last work was finishing is honoured, and no report is returned). A caller must render the run-level
outcome - a report, or ``CancelledError``/an error - and must never infer success from per-source
states.

Thread affinity
---------------
``compare``/``check_all``/``check_connection`` must be *called* on the event-loop thread, because
the call claims the session. Called elsewhere they raise ``RuntimeError`` and leave the session
untouched. From another thread, schedule a small coroutine that makes the call on the loop.
(``cancel()`` is the exception: it is safe from any thread.)

A run begins when ``compare()``/``check_all()``/``check_connection()`` is *called*, not when the
returned coroutine first runs, so a ``cancel()`` between creating the task and its first turn
aborts that run. The returned coroutine must therefore be awaited (or closed/dropped, which
releases the session).

No partial report is ever returned. A cancelled session can be used again.

No credential appears in an event, a log line or an exception message: credentials travel only as
``Dsn``, and anything shown comes from the library's already-redacted messages, scrubbed again here
as a second layer, or is a fixed string naming the exception type.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import re
import threading
import weakref
from collections.abc import Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

from .. import runner
from ..baseline import apply_baseline
from ..config.model import ComparerConfig, SourceRef
from ..config.secrets import Dsn, Secret
from ..diff.changelog import ChangelogOptions
from ..diff.ignores import IgnoreRuleSet
from ..diff.model import ComparisonReport, DiffOptions
from ..errors import MissingCredentialsError
from ..report.json_report import load_report
from ..runner import CaptureResult, ConnectionStatus
from .credentials import CredentialStore
from .errors import GuiError

log = logging.getLogger(__name__)

_R = TypeVar("_R")
_T = TypeVar("_T")


class _Claim:
    """One run's hold on the session, taken when the run is requested."""

    __slots__ = ("coro", "generation", "started", "work")

    def __init__(self, generation: int) -> None:
        self.generation = generation
        self.started = False
        self.coro: weakref.ref[Coroutine[Any, Any, Any]] | None = None
        self.work: Coroutine[Any, Any, Any] | None = None


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
        self._claim_held: _Claim | None = None

    # -- connection checks -------------------------------------------------------------------

    def _ssh_passphrase(self, source: SourceRef) -> Secret | None:
        """The key passphrase for this source's gateway, if its config names one.

        Keychain first then environment, exactly as the DSN is resolved: a passphrase stored in the
        desktop application is a passphrase the CLI still will not see.
        """
        ref = source.ssh
        if ref is None or not ref.passphrase_env:
            return None
        return self._credentials.resolve_secret(ref.passphrase_env)

    def check_connection(self, label: str) -> Coroutine[Any, Any, ConnectionStatus]:
        """Check one source. A failure is returned; an unknown label raises ``GuiError``.

        Guarded and cancellable exactly like ``check_all``.
        """
        source = self._source(label)
        claim = self._claim()
        work = self._check_sources([source])

        async def first() -> ConnectionStatus:
            return (await self._run(claim, work))[0]

        return self._watched(claim, work, first())

    def check_all(self) -> Coroutine[Any, Any, list[ConnectionStatus]]:
        """Check the master and every target, in configuration order.

        Bounded by ``max_workers`` (one at a time when ``parallel`` is off) and stoppable with
        ``cancel()``, exactly like ``compare``; cancelling raises ``CancelledError``.
        """
        claim = self._claim()
        work = self._check_sources([self._config.master, *self._config.targets])
        return self._watched(claim, work, self._run(claim, work))

    def _watched(
        self, claim: _Claim, work: Coroutine[Any, Any, Any], coro: Coroutine[Any, Any, _T]
    ) -> Coroutine[Any, Any, _T]:
        """Release the claim if ``coro`` is dropped or cancelled before it ever ran."""
        # Weak: a strong reference here would keep an abandoned coroutine alive forever.
        claim.coro, claim.work = weakref.ref(coro), work
        weakref.finalize(coro, self._abandon, claim, work)
        return coro

    async def _run(self, claim: _Claim, work: Coroutine[Any, Any, _T]) -> _T:
        claim.started = True
        self._task = asyncio.current_task()
        try:
            return await work
        finally:
            self._end()

    async def _check_sources(self, sources: list[SourceRef]) -> list[ConnectionStatus]:
        options = self._config.options
        workers = min(len(sources), options.max_workers) if options.parallel else 1
        done = await self._drive(
            sources,
            workers,
            self._check_blocking,
            lambda source, exc: ConnectionStatus(
                label=source.label,
                ok=False,
                error=f"{source.label}: check failed unexpectedly ({type(exc).__name__})",
            ),
            lambda status: (SourceState.CAPTURED if status.ok else SourceState.FAILED, None),
            announce=False,
        )
        if any(source.label not in done for source in sources):
            raise asyncio.CancelledError
        return [done[source.label] for source in sources]

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
                ssh_passphrase=self._ssh_passphrase(source),
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

    def compare(
        self,
        *,
        ignores: IgnoreRuleSet | None = None,
        changelog_options: ChangelogOptions | None = None,
        skip_liquibase: bool = False,
        sequential: bool = False,
        baseline: ComparisonReport | None = None,
        show_cosmetic: bool = False,
    ) -> Coroutine[Any, Any, ComparisonReport]:
        """Capture every source and build the report.

        Raises ``asyncio.CancelledError`` if cancelled, including a cancel that arrives at any
        point before the report would be returned; never returns a partial or a finished report to
        a cancelled run. One source failing does not stop the others: it becomes a skipped target.
        """
        claim = self._claim()
        work = self._compare(
            ignores, changelog_options, skip_liquibase, sequential, baseline, show_cosmetic
        )
        return self._watched(claim, work, self._run(claim, work))

    def _diff_options(self, show_cosmetic: bool) -> DiffOptions:
        """The same options the command line builds, plus the one flag only a caller knows.

        ``show_cosmetic`` has no home in the config file: it is a per-run choice, exactly as
        ``--show-cosmetic`` is on the command line.
        """
        options = self._config.options
        return DiffOptions(
            ignore_column_order=options.ignore_column_order,
            include_owners=options.include_owners,
            include_comments=options.include_comments,
            include_grants=options.include_grants,
            show_cosmetic=show_cosmetic,
        )

    async def _compare(
        self,
        ignores: IgnoreRuleSet | None,
        changelog_options: ChangelogOptions | None,
        skip_liquibase: bool,
        sequential: bool,
        baseline: ComparisonReport | None,
        show_cosmetic: bool = False,
    ) -> ComparisonReport:
        captures = await self._capture_sources(sequential, skip_liquibase)
        report = await self._uncancellable(
            asyncio.ensure_future(
                asyncio.to_thread(
                    runner.build_report,
                    self._config,
                    captures,
                    ignores=ignores,
                    changelog_options=changelog_options,
                    diff_options=self._diff_options(show_cosmetic),
                )
            )
        )
        self._raise_if_cancelled()
        if baseline is not None:
            report = apply_baseline(report, baseline).report
        return report

    def _claim(self) -> _Claim:
        # Resolve everything that can fail before touching any state, so a failure (notably being
        # called off the event-loop thread) leaves the session exactly as it was.
        loop = asyncio.get_running_loop()
        if self._running:
            held = self._claim_held
            if held is not None and self._never_ran(held):
                # A task cancelled before its first turn closes the coroutine without running its
                # body, so nothing would ever release the claim. Detected here rather than left to
                # garbage collection.
                self._abandon(held, held.work)
            else:
                raise GuiError("a check or comparison is already running")
        claim = _Claim(self._generation + 1)
        self._running = True
        self._generation = claim.generation
        self._cancel_requested.clear()
        self._loop = loop
        self._claim_held = claim
        return claim

    @staticmethod
    def _never_ran(claim: _Claim) -> bool:
        """Whether the run's coroutine can no longer run its body: dropped, or closed unstarted."""
        if claim.started or claim.coro is None:
            return False
        coro = claim.coro()
        return coro is None or inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED

    def _abandon(self, claim: _Claim, work: Coroutine[Any, Any, Any] | None) -> None:
        """Release a claim whose coroutine was dropped or cancelled before it ever ran."""
        if not claim.started and self._running and claim.generation == self._generation:
            self._end()
        if work is not None:
            work.close()

    def _end(self) -> None:
        self._task = None
        self._running = False

    def _raise_if_cancelled(self) -> None:
        if self._cancel_requested.is_set():
            raise asyncio.CancelledError

    async def _uncancellable(self, future: asyncio.Future[_T]) -> _T:
        """Wait for ``future`` whatever is delivered meanwhile; a cancellation becomes the flag.

        Abandoning the wait would free the session while a thread still runs. The absorbed
        cancellation is not lost: the flag makes the run end in ``CancelledError`` afterwards.
        """
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                self._cancel_requested.set()
        return future.result()

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
            await self._uncancellable(
                asyncio.ensure_future(asyncio.to_thread(pool.shutdown, True, cancel_futures=True))
            )
        self._raise_if_cancelled()
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
                ssh_passphrase=self._ssh_passphrase(source),
                exclude_schemas=self._config.exclude_schemas,
                options=runner.connection_options(self._config),
                skip_liquibase=skip_liquibase,
                # No opt-out in the GUI: what it shows is meant to be shareable.
                redact_literals=True,
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
        except BaseException as exc:
            # A consumer raising CancelledError is a consumer fault, not cancellation: real
            # cancellation arrives at an await point. Whatever the consumer does, the run must
            # still settle every source.
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
