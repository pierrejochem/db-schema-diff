"""Orchestration, with the database stubbed.

The window must never freeze, one unreachable source must not stop the others, and a cancelled run
must not produce a half-populated report that looks complete.

Nothing here sleeps to arrange a race. Worker threads block on ``threading.Event`` gates that the
test opens, so every interleaving asserted below is forced rather than hoped for.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import logging
import threading
from collections import defaultdict
from typing import Any, ClassVar
from unittest import mock

import pytest

from db_schema_diff.config.model import ComparerConfig
from db_schema_diff.errors import ProbeError
from db_schema_diff.gui.credentials import CredentialStore
from db_schema_diff.gui.errors import GuiError
from db_schema_diff.gui.session import ProgressEvent, Session, SourceState
from db_schema_diff.runner import CaptureResult, ConnectionStatus, TunnelStatus
from tests.support.builders import col, inventory, table

CONFIG = ComparerConfig.model_validate(
    {
        "version": 1,
        "name": "invoicing",
        "master": {"label": "prod", "dsn_env": "PROD_DSN"},
        "targets": [
            {"label": "qa", "dsn_env": "QA_DSN"},
            {"label": "dev", "dsn_env": "DEV_DSN"},
        ],
    }
)

SECRET = "s3cretPassw0rd"
DSN = f"postgresql://u:{SECRET}@h/db"
CAPTURE = "db_schema_diff.gui.session.runner.capture"
CHECK = "db_schema_diff.gui.session.runner.check_connection"
TERMINAL = {SourceState.CAPTURED, SourceState.FAILED, SourceState.CANCELLED}
LABELS = ("prod", "qa", "dev")


def store() -> CredentialStore:
    return CredentialStore(
        backend=None,
        environ={"PROD_DSN": DSN, "QA_DSN": DSN, "DEV_DSN": DSN},
        _import_keyring=False,
    )


def session(**kwargs: Any) -> tuple[Session, list[ProgressEvent]]:
    events: list[ProgressEvent] = []
    return Session(CONFIG, store(), on_progress=events.append, **kwargs), events


def ok_capture(label: str) -> CaptureResult:
    return CaptureResult(
        label=label, inventory=inventory(table("public", "t", cols=[col("c")]), label=label)
    )


def succeed(source: Any, *args: Any, **kwargs: Any) -> CaptureResult:
    return ok_capture(source.label)


def by_label(events: list[ProgressEvent]) -> dict[str, list[SourceState]]:
    grouped: dict[str, list[SourceState]] = defaultdict(list)
    for event in events:
        grouped[event.label].append(event.state)
    return grouped


def assert_every_source_ended_exactly_once(events: list[ProgressEvent]) -> None:
    grouped = by_label(events)
    assert set(grouped) == set(LABELS)
    for label, states in grouped.items():
        terminal = [s for s in states if s in TERMINAL]
        assert len(terminal) == 1, f"{label}: {states}"
        assert states[-1] in TERMINAL, f"{label} left unfinished: {states}"


def assert_no_secret(*things: object) -> None:
    for thing in things:
        assert SECRET not in str(thing), f"credential leaked into {thing!r}"
        assert DSN not in str(thing)


class Gate:
    """Holds worker threads inside ``capture`` until the test lets them go."""

    def __init__(self, expected: int) -> None:
        self.entered = threading.Semaphore(0)
        self.release = threading.Event()
        self.expected = expected
        self.started: list[str] = []
        self.finished: list[str] = []
        self._lock = threading.Lock()

    def capture(self, source: Any, *args: Any, **kwargs: Any) -> CaptureResult:
        with self._lock:
            self.started.append(source.label)
        self.entered.release()
        assert self.release.wait(10), "test never released the gate"
        with self._lock:
            self.finished.append(source.label)
        return ok_capture(source.label)

    async def all_entered(self) -> None:
        for _ in range(self.expected):
            assert await asyncio.wait_for(asyncio.to_thread(self.entered.acquire, True, 10), 15)


async def until(predicate: Any) -> None:
    """Yield to the loop until ``predicate()``; bounded so a bug fails instead of hanging."""

    async def spin() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(spin(), 10)


def many_sources(count: int, *, max_workers: int, parallel: bool = True) -> ComparerConfig:
    return ComparerConfig.model_validate(
        {
            "version": 1,
            "name": "many",
            "master": {"label": "s0", "dsn_env": "DSN_0"},
            "targets": [{"label": f"s{i}", "dsn_env": f"DSN_{i}"} for i in range(1, count)],
            "options": {"max_workers": max_workers, "parallel": parallel},
        }
    )


def many_store(count: int) -> CredentialStore:
    return CredentialStore(
        backend=None, environ={f"DSN_{i}": DSN for i in range(count)}, _import_keyring=False
    )


class PeakCounter:
    """Counts how many calls are live at once. Each call holds briefly on a 2-party barrier."""

    def __init__(self) -> None:
        self.live = 0
        self.peak = 0
        self._lock = threading.Lock()
        self._pair = threading.Barrier(2, timeout=0.3)

    def call(self, result: Any) -> Any:
        def run(source: Any, *args: Any, **kwargs: Any) -> Any:
            with self._lock:
                self.live += 1
                self.peak = max(self.peak, self.live)
            try:
                with contextlib.suppress(threading.BrokenBarrierError):
                    self._pair.wait()
                return result(source)
            finally:
                with self._lock:
                    self.live -= 1

        return run


class TestProgress:
    @pytest.mark.asyncio
    async def test_every_source_reports_capturing_then_captured(self):
        subject, events = session()
        with mock.patch(CAPTURE, side_effect=succeed):
            await subject.compare()
        for label in LABELS:
            states = by_label(events)[label]
            assert states == [SourceState.CAPTURING, SourceState.CAPTURED]

    @pytest.mark.asyncio
    async def test_a_failing_source_does_not_stop_the_others(self):
        def capture(source, *args, **kwargs):
            if source.label == "qa":
                return CaptureResult(label="qa", error="qa: cannot connect (host=h)")
            return ok_capture(source.label)

        subject, events = session()
        with mock.patch(CAPTURE, side_effect=capture):
            report = await subject.compare()

        assert by_label(events)["qa"][-1] is SourceState.FAILED
        assert by_label(events)["dev"][-1] is SourceState.CAPTURED
        # The report says so rather than presenting a partial comparison as clean.
        assert report.probe_failed is True
        assert_every_source_ended_exactly_once(events)

    @pytest.mark.asyncio
    async def test_progress_is_reported_before_the_work_starts(self):
        # Otherwise the window shows nothing until the first capture finishes.
        subject, events = session()
        seen_at_capture: list[int] = []

        def capture(source, *args, **kwargs):
            seen_at_capture.append(len(events))
            return ok_capture(source.label)

        with mock.patch(CAPTURE, side_effect=capture):
            await subject.compare(sequential=True)
        assert events[0].state is SourceState.CAPTURING
        assert seen_at_capture[0] >= 1

    @pytest.mark.asyncio
    async def test_a_source_that_raises_unexpectedly_is_failed_and_the_rest_finish(self):
        # runner.capture only converts ComparerError; anything else is a bug that must still not
        # take the other sources down or leave this one hanging in CAPTURING.
        def capture(source, *args, **kwargs):
            if source.label == "qa":
                raise RuntimeError(f"boom while using {DSN}")
            return ok_capture(source.label)

        subject, events = session()
        with mock.patch(CAPTURE, side_effect=capture):
            report = await subject.compare()

        grouped = by_label(events)
        assert grouped["qa"][-1] is SourceState.FAILED
        assert grouped["prod"][-1] is SourceState.CAPTURED
        assert grouped["dev"][-1] is SourceState.CAPTURED
        assert report.probe_failed is True
        assert_every_source_ended_exactly_once(events)

    @pytest.mark.asyncio
    async def test_a_missing_credential_fails_that_source_only(self):
        environ = {"PROD_DSN": DSN, "DEV_DSN": DSN}
        events: list[ProgressEvent] = []
        subject = Session(
            CONFIG,
            CredentialStore(backend=None, environ=environ, _import_keyring=False),
            on_progress=events.append,
        )
        with mock.patch(CAPTURE, side_effect=succeed):
            report = await subject.compare()

        failed = [e for e in events if e.state is SourceState.FAILED]
        assert [e.label for e in failed] == ["qa"]
        assert "QA_DSN" in (failed[0].detail or "")
        assert report.probe_failed is True
        assert_every_source_ended_exactly_once(events)

    @pytest.mark.asyncio
    async def test_a_consumer_that_raises_cannot_fail_the_comparison(self):
        calls: list[ProgressEvent] = []

        def broken(event: ProgressEvent) -> None:
            calls.append(event)
            raise ValueError(f"consumer bug {DSN}")

        subject = Session(CONFIG, store(), on_progress=broken)
        with mock.patch(CAPTURE, side_effect=succeed):
            report = await subject.compare()
        assert len(calls) == 6  # still told of every change, despite raising each time
        assert [t.target_label for t in report.targets] == ["qa", "dev"]
        assert report.probe_failed is False
        assert all(t.failed is None for t in report.targets)

    @pytest.mark.asyncio
    async def test_a_consumer_raising_a_base_exception_still_settles_every_source(self):
        class Fatal(BaseException):
            pass

        seen: list[ProgressEvent] = []

        def broken(event: ProgressEvent) -> None:
            seen.append(event)
            raise Fatal

        subject = Session(CONFIG, store(), on_progress=broken)
        with mock.patch(CAPTURE, side_effect=succeed):
            await subject.compare()
        assert_every_source_ended_exactly_once(seen)

    @pytest.mark.asyncio
    async def test_a_base_exception_from_capture_fails_that_source_only(self):
        class Fatal(BaseException):
            pass

        def capture(source, *args, **kwargs):
            if source.label == "qa":
                raise Fatal(f"fatal {DSN}")
            return ok_capture(source.label)

        subject, events = session()
        with mock.patch(CAPTURE, side_effect=capture):
            report = await subject.compare()
        assert by_label(events)["qa"][-1] is SourceState.FAILED
        assert by_label(events)["dev"][-1] is SourceState.CAPTURED
        assert report.probe_failed is True
        assert_every_source_ended_exactly_once(events)
        assert_no_secret(events)


class TestCallbackThread:
    """The consumer is not assumed thread-safe: it is only ever called on the loop thread."""

    @pytest.mark.asyncio
    async def test_progress_is_only_delivered_on_the_loop_thread(self):
        loop_thread = threading.get_ident()
        callers: list[int] = []
        worker_threads: list[int] = []

        def on_progress(event: ProgressEvent) -> None:
            callers.append(threading.get_ident())

        def capture(source, *args, **kwargs):
            worker_threads.append(threading.get_ident())
            if source.label == "qa":
                return CaptureResult(label="qa", error="qa: cannot connect")
            return ok_capture(source.label)

        subject = Session(CONFIG, store(), on_progress=on_progress)
        with mock.patch(CAPTURE, side_effect=capture):
            await subject.compare()

        assert callers, "no events at all"
        assert set(callers) == {loop_thread}
        assert loop_thread not in worker_threads

    @pytest.mark.asyncio
    async def test_cancelled_events_are_also_on_the_loop_thread(self):
        loop_thread = threading.get_ident()
        callers: list[int] = []
        gate = Gate(expected=1)
        subject = Session(
            CONFIG, store(), on_progress=lambda e: callers.append(threading.get_ident())
        )
        with mock.patch(CAPTURE, side_effect=gate.capture):
            task = asyncio.create_task(subject.compare(sequential=True))
            await gate.all_entered()
            subject.cancel()
            await until(lambda: len(callers) >= 4)
            gate.release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert set(callers) == {loop_thread}


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_blocking_work_runs_off_the_loop_thread(self):
        """The one concurrency invariant: nothing blocking on the loop thread."""
        loop_thread = threading.get_ident()
        seen: list[int] = []

        def capture(source, *args, **kwargs):
            seen.append(threading.get_ident())
            return ok_capture(source.label)

        subject, _ = session()
        with mock.patch(CAPTURE, side_effect=capture):
            await subject.compare()
        assert len(seen) == 3
        assert loop_thread not in seen

    @pytest.mark.asyncio
    async def test_sequential_captures_one_at_a_time(self):
        # Each capture waits for a partner. Sequential mode can never supply one, so the barrier
        # opening would prove two captures were live at once.
        barrier = threading.Barrier(2, timeout=0.2)
        opened: list[str] = []

        def capture(source, *args, **kwargs):
            with contextlib.suppress(threading.BrokenBarrierError):
                barrier.wait()
                opened.append(source.label)
            return ok_capture(source.label)

        subject, _ = session()
        with mock.patch(CAPTURE, side_effect=capture):
            await subject.compare(sequential=True)
        assert opened == []

    @pytest.mark.asyncio
    async def test_the_same_harness_does_detect_overlap_when_parallel(self):
        # Guards the test above: proves that barrier can open, so its silence means something.
        barrier = threading.Barrier(3, timeout=10)
        opened: list[str] = []

        def capture(source, *args, **kwargs):
            with contextlib.suppress(threading.BrokenBarrierError):
                barrier.wait()
                opened.append(source.label)
            return ok_capture(source.label)

        subject, _ = session()
        with mock.patch(CAPTURE, side_effect=capture):
            await subject.compare()
        assert len(opened) == 3

    @pytest.mark.asyncio
    async def test_parallel_mode_really_overlaps_captures(self):
        # All three must be inside capture at once: a barrier that only opens when they are.
        barrier = threading.Barrier(3, timeout=10)
        opened: list[str] = []

        def capture(source, *args, **kwargs):
            barrier.wait()
            opened.append(source.label)
            return ok_capture(source.label)

        subject, events = session()
        with mock.patch(CAPTURE, side_effect=capture):
            report = await subject.compare()
        assert sorted(opened) == sorted(LABELS)
        assert report.probe_failed is False
        assert all(s[-1] is SourceState.CAPTURED for s in by_label(events).values())

    @pytest.mark.asyncio
    async def test_max_workers_caps_compare(self):
        config = many_sources(9, max_workers=2)
        peak = PeakCounter()
        subject = Session(config, many_store(9))
        with mock.patch(CAPTURE, side_effect=peak.call(lambda s: ok_capture(s.label))):
            await subject.compare()
        assert peak.peak == 2


class TestCancellation:
    """Cancelling mid-capture: real, ordered, and restartable."""

    @pytest.mark.asyncio
    async def test_cancelling_raises_rather_than_returning_a_partial_report(self):
        gate = Gate(expected=3)
        subject, events = session()
        with mock.patch(CAPTURE, side_effect=gate.capture):
            task = asyncio.create_task(subject.compare())
            await gate.all_entered()
            subject.cancel()
            await until(lambda: task.cancelling() > 0 or task.done())
            gate.release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert_every_source_ended_exactly_once(events)
        assert all(s[-1] is SourceState.CANCELLED for s in by_label(events).values())

    @pytest.mark.asyncio
    async def test_queued_sources_never_start_and_in_flight_ones_are_waited_for(self):
        gate = Gate(expected=1)
        subject, events = session()
        with mock.patch(CAPTURE, side_effect=gate.capture):
            task = asyncio.create_task(subject.compare(sequential=True))
            await gate.all_entered()  # prod is in flight; qa and dev are queued
            subject.cancel()

            # The window learns at once, while the thread is still inside the database call.
            await until(lambda: len([e for e in events if e.state is SourceState.CANCELLED]) == 3)
            assert not task.done(), "compare returned while a capture was still running"
            assert gate.finished == []

            # Still draining: a second run must not overlap the first.
            with pytest.raises(GuiError, match="already running"):
                await subject.compare()

            gate.release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        # By the time it raised, the in-flight thread had ended, and nothing else ever started.
        assert gate.finished == ["prod"]
        assert gate.started == ["prod"]
        assert_every_source_ended_exactly_once(events)
        # The finished-but-discarded capture did not turn into a CAPTURED event.
        assert not [e for e in events if e.state is SourceState.CAPTURED]

    @pytest.mark.asyncio
    async def test_a_second_cancel_during_the_drain_does_not_release_the_session(self):
        """Double-clicking Cancel must not let a new run start over a capture still querying."""
        gate = Gate(expected=1)
        subject, events = session()
        with mock.patch(CAPTURE, side_effect=gate.capture):
            task = asyncio.create_task(subject.compare(sequential=True))
            await gate.all_entered()
            subject.cancel()
            await until(lambda: len([e for e in events if e.state is SourceState.CANCELLED]) == 3)
            assert not task.done()

            subject.cancel()  # the impatient second click
            for _ in range(10):  # let the threadsafe callback run
                await asyncio.sleep(0)
            assert not task.done(), "second cancel abandoned the drain"
            with pytest.raises(GuiError, match="already running"):
                await subject.compare(sequential=True)
            assert gate.started == ["prod"]

            gate.release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert gate.finished == ["prod"]

            # Only now is the session free, and a fresh run does not overlap the old capture.
            events.clear()
            gate2 = Gate(expected=0)
            gate2.release.set()
        with mock.patch(CAPTURE, side_effect=gate2.capture):
            report = await subject.compare(sequential=True)
        assert [t.target_label for t in report.targets] == ["qa", "dev"]

    @pytest.mark.asyncio
    async def test_parallel_cancel_reports_every_source_once(self):
        gate = Gate(expected=3)
        subject, events = session()
        with mock.patch(CAPTURE, side_effect=gate.capture):
            task = asyncio.create_task(subject.compare())
            await gate.all_entered()
            subject.cancel()
            await until(lambda: len([e for e in events if e.state is SourceState.CANCELLED]) == 3)
            gate.release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert sorted(gate.finished) == sorted(LABELS)
        assert_every_source_ended_exactly_once(events)

    @pytest.mark.asyncio
    async def test_a_source_that_finished_before_the_cancel_keeps_its_result(self):
        # Terminal states are final: a captured source is not rewritten as cancelled.
        release_dev = threading.Event()
        prod_done = threading.Event()

        def capture(source, *args, **kwargs):
            if source.label == "prod":
                prod_done.set()
                return ok_capture("prod")
            assert release_dev.wait(10)
            return ok_capture(source.label)

        subject, events = session()
        with mock.patch(CAPTURE, side_effect=capture):
            task = asyncio.create_task(subject.compare())
            await asyncio.to_thread(prod_done.wait, 10)
            await until(lambda: SourceState.CAPTURED in by_label(events).get("prod", []))
            subject.cancel()
            await until(lambda: SourceState.CANCELLED in by_label(events).get("dev", []))
            release_dev.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert by_label(events)["prod"] == [SourceState.CAPTURING, SourceState.CAPTURED]
        assert by_label(events)["dev"][-1] is SourceState.CANCELLED
        assert_every_source_ended_exactly_once(events)

    @pytest.mark.asyncio
    async def test_cancel_from_another_thread(self):
        gate = Gate(expected=3)
        subject, events = session()
        with mock.patch(CAPTURE, side_effect=gate.capture):
            task = asyncio.create_task(subject.compare())
            await gate.all_entered()
            await asyncio.to_thread(subject.cancel)
            gate.release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert_every_source_ended_exactly_once(events)

    @pytest.mark.asyncio
    async def test_a_cancelled_session_can_run_again(self):
        subject, events = session()
        subject.cancel()
        with mock.patch(CAPTURE, side_effect=succeed):
            report = await subject.compare()
        assert [t.target_label for t in report.targets] == ["qa", "dev"]
        assert report.probe_failed is False
        assert all(s[-1] is SourceState.CAPTURED for s in by_label(events).values())

    @pytest.mark.asyncio
    async def test_a_session_cancelled_mid_run_can_run_again_with_fresh_events(self):
        gate = Gate(expected=3)
        subject, events = session()
        with mock.patch(CAPTURE, side_effect=gate.capture):
            task = asyncio.create_task(subject.compare())
            await gate.all_entered()
            subject.cancel()
            gate.release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        events.clear()
        with mock.patch(CAPTURE, side_effect=succeed):
            report = await subject.compare()
        assert report.targets
        assert all(
            s == [SourceState.CAPTURING, SourceState.CAPTURED] for s in by_label(events).values()
        )

    def test_a_worker_picking_up_a_cancelled_job_does_not_query(self):
        subject, _ = session()
        subject.cancel()
        with mock.patch(CAPTURE, side_effect=succeed) as capture:
            assert subject._capture_blocking(CONFIG.targets[0], False) is None
        capture.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_late_cancel_does_not_kill_the_next_run(self):
        subject, events = session()
        with mock.patch(CAPTURE, side_effect=succeed):
            await subject.compare()
            subject.cancel()  # nothing running: must be inert, not queued against the next run
            events.clear()
            report = await subject.compare()
        assert [t.target_label for t in report.targets] == ["qa", "dev"]
        assert all(
            s == [SourceState.CAPTURING, SourceState.CAPTURED] for s in by_label(events).values()
        )

    @pytest.mark.asyncio
    async def test_a_consumer_cancelling_on_captured_stops_the_next_source_starting(self):
        # Sequential: prod releases its slot before CAPTURED is emitted, so qa is already woken
        # when the consumer cancels. Without the start-of-source check qa would visibly begin
        # after Cancel.
        captured_labels: list[str] = []
        holder: dict[str, Session] = {}

        def on_progress(event: ProgressEvent) -> None:
            events.append(event)
            if event.state is SourceState.CAPTURED:
                holder["s"].cancel()

        events: list[ProgressEvent] = []
        subject = Session(CONFIG, store(), on_progress=on_progress)
        holder["s"] = subject

        def capture(source, *args, **kwargs):
            captured_labels.append(source.label)
            return ok_capture(source.label)

        with mock.patch(CAPTURE, side_effect=capture), pytest.raises(asyncio.CancelledError):
            await subject.compare(sequential=True)
        assert captured_labels == ["prod"]
        assert by_label(events)["qa"] == [SourceState.CANCELLED]
        assert by_label(events)["dev"] == [SourceState.CANCELLED]
        assert_every_source_ended_exactly_once(events)

    @pytest.mark.asyncio
    async def test_cancelling_the_task_directly_is_treated_the_same(self):
        gate = Gate(expected=3)
        subject, events = session()
        with mock.patch(CAPTURE, side_effect=gate.capture):
            task = asyncio.create_task(subject.compare())
            await gate.all_entered()
            task.cancel()
            await until(lambda: len([e for e in events if e.state is SourceState.CANCELLED]) == 3)
            gate.release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert_every_source_ended_exactly_once(events)
        # And the session is usable afterwards.
        with mock.patch(CAPTURE, side_effect=succeed):
            assert (await subject.compare()).targets


class TestCancelNeverYieldsResults:
    """A Cancel pressed while a run is live must not be answered with a finished report."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("twice", [False, True])
    async def test_compare_cancel_sweep(self, twice):
        outcomes = {}
        for k in range(0, 60):
            subject, _ = session()
            with mock.patch(CAPTURE, side_effect=succeed):
                task = asyncio.create_task(subject.compare())
                for _ in range(k):
                    await asyncio.sleep(0)
                live = not task.done()
                subject.cancel()
                if twice:
                    subject.cancel()
                try:
                    await task
                    outcomes[k] = (live, "report")
                except asyncio.CancelledError:
                    outcomes[k] = (live, "cancelled")
        silent = [k for k, (live, what) in outcomes.items() if live and what == "report"]
        assert silent == [], f"cancel answered with a full report at loop turns {silent}"
        assert any(what == "cancelled" for _, what in outcomes.values())

    @pytest.mark.asyncio
    async def test_check_all_cancel_sweep(self):
        silent = []
        for k in range(0, 60):
            subject, _ = session()
            with mock.patch(
                CHECK, side_effect=lambda s, *a, **kw: ConnectionStatus(label=s.label, ok=True)
            ):
                task = asyncio.create_task(subject.check_all())
                for _ in range(k):
                    await asyncio.sleep(0)
                live = not task.done()
                subject.cancel()
                subject.cancel()
                try:
                    await task
                    if live:
                        silent.append(k)
                except asyncio.CancelledError:
                    pass
        assert silent == []

    @pytest.mark.asyncio
    async def test_a_cancel_before_the_coroutine_first_runs_aborts_that_run(self):
        subject, events = session()
        calls: list[str] = []

        def capture(source, *args, **kwargs):
            calls.append(source.label)
            return ok_capture(source.label)

        with mock.patch(CAPTURE, side_effect=capture):
            task = asyncio.create_task(subject.compare())
            subject.cancel()  # the task has not had its first turn
            with pytest.raises(asyncio.CancelledError):
                await task
            assert calls == []
            assert_every_source_ended_exactly_once(events)
            assert all(s[-1] is SourceState.CANCELLED for s in by_label(events).values())
            # and the session is usable afterwards
            assert (await subject.compare()).targets

    @pytest.mark.asyncio
    async def test_a_task_cancelled_before_it_ever_ran_does_not_wedge_the_session(self):
        subject, _ = session()
        with mock.patch(CAPTURE, side_effect=succeed):
            task = asyncio.create_task(subject.compare())
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            del task
            gc.collect()
            assert (await subject.compare()).targets

    @pytest.mark.asyncio
    async def test_cancel_during_build_report_keeps_the_session_busy_until_it_ends(self):
        entered = threading.Event()
        release = threading.Event()
        running: list[int] = []

        def slow_build(*args, **kwargs):
            running.append(1)
            entered.set()
            assert release.wait(10)
            running.append(0)
            raise AssertionError("unreachable result must be discarded")

        subject, _ = session()
        with (
            mock.patch(CAPTURE, side_effect=succeed),
            mock.patch("db_schema_diff.gui.session.runner.build_report", slow_build),
        ):
            task = asyncio.create_task(subject.compare())
            assert await asyncio.to_thread(entered.wait, 10)
            subject.cancel()
            for _ in range(10):
                await asyncio.sleep(0)
            assert not task.done()
            with pytest.raises(GuiError, match="already running"):
                await subject.compare()
            release.set()
            with pytest.raises((asyncio.CancelledError, AssertionError)):
                await task
        assert running == [1, 0]

    @pytest.mark.asyncio
    async def test_a_single_source_check_is_guarded_and_cancellable(self):
        gate = threading.Event()
        entered = threading.Semaphore(0)
        live: list[str] = []

        def check(source, *args, **kwargs):
            live.append(source.label)
            entered.release()
            assert gate.wait(10)
            return ConnectionStatus(label=source.label, ok=True)

        subject, _ = session()
        with mock.patch(CHECK, side_effect=check):
            task = asyncio.create_task(subject.check_connection("qa"))
            assert await asyncio.to_thread(entered.acquire, True, 10)
            subject.cancel()
            for _ in range(10):
                await asyncio.sleep(0)
            assert not task.done()
            with pytest.raises(GuiError, match="already running"):
                await subject.check_connection("dev")
            gate.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert live == ["qa"]


class TestOffLoopCalls:
    """Called on the wrong thread, the entry points raise and leave the session untouched."""

    CALLS: ClassVar[dict[str, Any]] = {
        "compare": lambda s: s.compare(),
        "check_all": lambda s: s.check_all(),
        "check_connection": lambda s: s.check_connection("qa"),
    }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("which", ["compare", "check_all", "check_connection"])
    async def test_an_off_loop_call_raises_and_leaves_the_session_usable(self, which):
        subject, _ = session()
        generation = subject._generation

        def call_off_loop() -> None:
            with pytest.raises(RuntimeError):
                self.CALLS[which](subject)

        await asyncio.to_thread(call_off_loop)
        assert not subject._running
        assert subject._generation == generation
        with mock.patch(CAPTURE, side_effect=succeed):
            for _ in range(3):
                assert (await subject.compare()).targets

    @pytest.mark.asyncio
    async def test_a_failure_part_way_through_claiming_leaves_no_residue(self):
        subject, _ = session()
        before = (subject._running, subject._generation, subject._loop, subject._claim_held)
        with (
            mock.patch("db_schema_diff.gui.session._Claim", side_effect=ValueError("late")),
            pytest.raises(ValueError, match="late"),
        ):
            subject.compare()
        assert (subject._running, subject._generation, subject._loop, subject._claim_held) == before
        with mock.patch(CAPTURE, side_effect=succeed):
            assert (await subject.compare()).targets


class TestDroppedCoroutine:
    """A coroutine created and never awaited must not strand the session."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("which", ["compare", "check_all", "check_connection"])
    async def test_dropped_then_collected_frees_the_session(self, which):
        subject, _ = session()
        call = {
            "compare": subject.compare,
            "check_all": subject.check_all,
            "check_connection": lambda: subject.check_connection("qa"),
        }[which]
        with pytest.warns(RuntimeWarning, match="never awaited"):
            call()  # dropped immediately
            gc.collect()
        with mock.patch(CAPTURE, side_effect=succeed):
            assert [t.target_label for t in (await subject.compare()).targets] == ["qa", "dev"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("which", ["compare", "check_all", "check_connection"])
    async def test_next_call_frees_it_with_the_finalizer_and_collection_disabled(self, which):
        # The next-call path alone has to carry the guarantee.
        subject, _ = session()
        call = {
            "compare": subject.compare,
            "check_all": subject.check_all,
            "check_connection": lambda: subject.check_connection("qa"),
        }[which]
        gc.disable()
        try:
            with (
                mock.patch("db_schema_diff.gui.session.weakref.finalize", new=lambda *a, **k: None),
                pytest.warns(RuntimeWarning, match="never awaited"),
            ):
                call()
                assert subject._running, "the finalizer is off; only the next call can free it"
                with mock.patch(CAPTURE, side_effect=succeed):
                    report = await subject.compare()
        finally:
            gc.enable()
        assert report.probe_failed is False
        assert not subject._running

    @pytest.mark.asyncio
    async def test_a_dropped_coroutine_does_not_make_later_runs_fail_forever(self):
        subject, _ = session()
        with pytest.warns(RuntimeWarning, match="never awaited"):
            subject.compare()
            gc.collect()
        with mock.patch(CAPTURE, side_effect=succeed):
            for _ in range(3):
                assert (await subject.compare()).targets

    @pytest.mark.asyncio
    async def test_a_coroutine_that_is_still_held_keeps_the_session_busy(self):
        # Alive and unawaited is indistinguishable from about-to-be-awaited: that is the limit.
        subject, _ = session()
        held = subject.compare()
        with pytest.raises(GuiError, match="already running"):
            subject.compare()
        with mock.patch(CAPTURE, side_effect=succeed):
            assert (await held).targets
            assert (await subject.compare()).targets


class TestConsumerCancelledError:
    @pytest.mark.asyncio
    async def test_a_consumer_raising_cancelled_error_strands_no_source(self):
        seen: list[ProgressEvent] = []

        def consumer(event: ProgressEvent) -> None:
            seen.append(event)
            raise asyncio.CancelledError

        subject = Session(CONFIG, store(), on_progress=consumer)
        with mock.patch(CAPTURE, side_effect=succeed):
            report = await subject.compare()
        assert_every_source_ended_exactly_once(seen)
        assert report.probe_failed is False

    @pytest.mark.asyncio
    async def test_a_consumer_raising_cancelled_error_during_a_real_cancel_strands_none(self):
        gate = Gate(expected=3)
        seen: list[ProgressEvent] = []

        def consumer(event: ProgressEvent) -> None:
            seen.append(event)
            raise asyncio.CancelledError

        subject = Session(CONFIG, store(), on_progress=consumer)
        with mock.patch(CAPTURE, side_effect=gate.capture):
            task = asyncio.create_task(subject.compare())
            await gate.all_entered()
            subject.cancel()
            await until(lambda: len([e for e in seen if e.state is SourceState.CANCELLED]) == 3)
            gate.release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert_every_source_ended_exactly_once(seen)


class TestNoCredentialLeaks:
    """Every path a failure can take, checked against a distinctive password."""

    @pytest.mark.asyncio
    async def test_library_error_text_is_scrubbed_again(self):
        def capture(source, *args, **kwargs):
            if source.label == "qa":
                return CaptureResult(label="qa", error=f"qa: driver said {DSN} and {SECRET}")
            return ok_capture(source.label)

        subject, events = session()
        with mock.patch(CAPTURE, side_effect=capture):
            report = await subject.compare()
        assert_no_secret(events, report.targets)

    @pytest.mark.asyncio
    async def test_a_short_password_is_scrubbed_from_library_text(self):
        short = "postgresql://u:pw1@h/db"
        environ = {"PROD_DSN": short, "QA_DSN": short, "DEV_DSN": short}
        events: list[ProgressEvent] = []
        subject = Session(
            CONFIG,
            CredentialStore(backend=None, environ=environ, _import_keyring=False),
            on_progress=events.append,
        )

        def capture(source, *args, **kwargs):
            if source.label == "qa":
                return CaptureResult(label="qa", error="qa: rejected password pw1 for u@h")
            return ok_capture(source.label)

        with mock.patch(CAPTURE, side_effect=capture):
            report = await subject.compare()
        shown = f"{events} {report.targets} {report.notes}"
        assert "pw1" not in shown
        assert "u@h" not in shown

    @pytest.mark.asyncio
    async def test_unexpected_exception_reaches_no_event_log_or_report(self, caplog):
        def capture(source, *args, **kwargs):
            raise RuntimeError(f"connect failed for {DSN}")

        subject, events = session()
        with caplog.at_level(logging.DEBUG), mock.patch(CAPTURE, side_effect=capture):
            report = await subject.compare()
        assert_no_secret(events, caplog.text, report.targets, report.notes)
        assert all(e.state is SourceState.FAILED for e in events if e.state in TERMINAL)

    @pytest.mark.asyncio
    async def test_library_failure_log_and_events_are_clean(self, caplog):
        def capture(source, *args, **kwargs):
            raise ProbeError(f"probe failed for {DSN}")

        subject, events = session()
        with caplog.at_level(logging.DEBUG), mock.patch(CAPTURE, side_effect=capture):
            await subject.compare()
        assert_no_secret(events, caplog.text)

    @pytest.mark.asyncio
    async def test_consumer_exception_is_not_logged_with_its_text(self, caplog):
        def broken(event: ProgressEvent) -> None:
            raise ValueError(f"consumer bug {DSN}")

        subject = Session(CONFIG, store(), on_progress=broken)
        with caplog.at_level(logging.DEBUG), mock.patch(CAPTURE, side_effect=succeed):
            await subject.compare()
        assert caplog.text, "the drop should be visible in the log"
        assert_no_secret(caplog.text)

    @pytest.mark.asyncio
    async def test_credential_lookup_failure_is_clean(self, caplog):
        class BrokenStore(CredentialStore):
            def resolve(self, env_name: str):
                raise OSError(f"keychain says {DSN}")

        events: list[ProgressEvent] = []
        subject = Session(
            CONFIG,
            BrokenStore(backend=None, environ={}, _import_keyring=False),
            on_progress=events.append,
        )
        with caplog.at_level(logging.DEBUG):
            await subject.compare()
            status = await subject.check_connection("qa")
        assert_no_secret(events, caplog.text, status)
        assert status.ok is False

    @pytest.mark.asyncio
    async def test_check_connection_failures_are_clean(self, caplog):
        subject, _ = session()
        with caplog.at_level(logging.DEBUG):
            with mock.patch(CHECK, side_effect=RuntimeError(f"boom {DSN}")):
                status = await subject.check_connection("qa")
            leaking = ConnectionStatus(label="qa", ok=False, error=f"library said {DSN}")
            with mock.patch(CHECK, return_value=leaking):
                scrubbed = await subject.check_connection("qa")
        assert status.ok is False
        assert_no_secret(status, scrubbed, caplog.text)

    def test_a_bad_baseline_message_quotes_neither_document_nor_secret(self, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text(f'{{"connection": "{DSN}", oops')
        with pytest.raises(GuiError) as info:
            Session.load_baseline(path)
        assert_no_secret(info.value)
        assert "baseline" in str(info.value)


class TestBaseline:
    """An approved report stops its own findings gating, and nothing else.

    This is what makes the tool adoptable against environments that have already drifted, so the GUI
    has to offer it rather than leaving it to the CLI.
    """

    @pytest.mark.asyncio
    async def test_a_baseline_moves_its_findings_out_of_the_way(self):
        def capture(source, *args, **kwargs):
            if source.label == "qa":
                # A differing schema, not an empty one: an empty target short-circuits to a single
                # note with zero findings, and a baseline would then have nothing to accept.
                return CaptureResult(
                    label="qa",
                    inventory=inventory(table("public", "t", cols=[col("c", "int8")]), label="qa"),
                )
            return ok_capture(source.label)

        subject, _ = session()
        with mock.patch(CAPTURE, side_effect=capture):
            first = await subject.compare()
        # The baseline is only meaningful if the first run actually found something.
        assert [f for t in first.targets for f in t.findings], "no findings to baseline"

        subject2, _ = session()
        with mock.patch(CAPTURE, side_effect=capture):
            second = await subject2.compare(baseline=first)

        # Everything the baseline already contained is set aside, auditably — and there has to
        # be something there, or this assertion would pass over an empty list.
        accepted = [f for t in second.targets for f in t.ignored]
        assert accepted, "the baseline accepted nothing"
        assert all(finding.ignored_by == "baseline" for finding in accepted)
        assert not [f for t in second.targets for f in t.findings]

    @pytest.mark.asyncio
    async def test_no_baseline_leaves_the_report_untouched(self):
        def capture(source, *args, **kwargs):
            if source.label == "qa":
                return CaptureResult(
                    label="qa",
                    inventory=inventory(table("public", "t", cols=[col("c", "int8")]), label="qa"),
                )
            return ok_capture(source.label)

        subject, _ = session()
        with mock.patch(CAPTURE, side_effect=capture):
            report = await subject.compare(baseline=None)
        assert [f for t in report.targets for f in t.findings], "nothing to leave untouched"
        assert all(target.ignored == () for target in report.targets)

    def test_loading_a_malformed_baseline_is_a_gui_error(self, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text("{}")
        with pytest.raises(GuiError, match="baseline"):
            Session.load_baseline(path)

    def test_loading_a_missing_baseline_is_a_gui_error(self, tmp_path):
        with pytest.raises(GuiError, match="baseline"):
            Session.load_baseline(tmp_path / "absent.json")


class TestCheckConnection:
    @pytest.mark.asyncio
    async def test_checking_one_source_returns_its_status(self):
        status = ConnectionStatus(label="qa", ok=True, server_version="15.19")
        subject, _ = session()
        with mock.patch(CHECK, return_value=status) as check:
            assert (await subject.check_connection("qa")).server_version == "15.19"
        source, dsn = check.call_args.args
        assert source.label == "qa"
        assert dsn.env_name == "QA_DSN"
        assert dsn.value == DSN

    @pytest.mark.asyncio
    async def test_check_all_honours_max_workers(self):
        # 9 sources at max_workers=2: a barrier wanting all 9 live at once must never open.
        config = many_sources(9, max_workers=2)
        barrier = threading.Barrier(9, timeout=0.2)
        opened: list[str] = []
        peak = PeakCounter()

        def check(source, *args, **kwargs):
            with contextlib.suppress(threading.BrokenBarrierError):
                barrier.wait()
                opened.append(source.label)
            return ConnectionStatus(label=source.label, ok=True)

        subject = Session(config, many_store(9))
        with mock.patch(CHECK, side_effect=peak.call(lambda s: check(s))):
            statuses = await subject.check_all()
        assert opened == []
        assert peak.peak == 2
        assert [s.label for s in statuses] == [f"s{i}" for i in range(9)]

    @pytest.mark.asyncio
    async def test_check_all_can_be_cancelled_and_waits_for_its_threads(self):
        config = many_sources(3, max_workers=1)
        entered = threading.Semaphore(0)
        release = threading.Event()
        started: list[str] = []
        finished: list[str] = []

        def check(source, *args, **kwargs):
            started.append(source.label)
            entered.release()
            assert release.wait(10)
            finished.append(source.label)
            return ConnectionStatus(label=source.label, ok=True)

        subject = Session(config, many_store(3))
        with mock.patch(CHECK, side_effect=check):
            task = asyncio.create_task(subject.check_all())
            assert await asyncio.to_thread(entered.acquire, True, 10)
            subject.cancel()
            for _ in range(10):
                await asyncio.sleep(0)
            assert not task.done()
            with pytest.raises(GuiError, match="already running"):
                await subject.check_all()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert started == finished == ["s0"]
        with mock.patch(
            CHECK, side_effect=lambda s, *a, **k: ConnectionStatus(label=s.label, ok=True)
        ):
            assert len(await subject.check_all()) == 3

    @pytest.mark.asyncio
    async def test_checking_all_sources_returns_one_status_each(self):
        subject, _ = session()
        with mock.patch(
            CHECK, side_effect=lambda s, *a, **k: ConnectionStatus(label=s.label, ok=True)
        ):
            statuses = await subject.check_all()
        assert [s.label for s in statuses] == list(LABELS)

    @pytest.mark.asyncio
    async def test_a_missing_credential_is_reported_as_a_failed_status(self):
        # Not raised: the Credentials tab shows one row per source.
        subject = Session(CONFIG, CredentialStore(backend=None, environ={}, _import_keyring=False))
        status = await subject.check_connection("qa")
        assert status.ok is False
        assert "QA_DSN" in (status.error or "")

    @pytest.mark.asyncio
    async def test_an_unknown_label_is_a_gui_error(self):
        subject, _ = session()
        with pytest.raises(GuiError, match="staging"):
            await subject.check_connection("staging")


class TestCaptureIsAlwaysRedacted:
    """The GUI has no opt-out, and nothing used to hold it to that.

    Flipping ``redact_literals=True`` to ``False`` in ``session.py`` passed all 616 GUI tests, the
    full suite and the integration suite. What the GUI shows is meant to be shareable — a reader
    screenshots the detail pane — so the keyword itself is the assertion.
    """

    @pytest.mark.asyncio
    async def test_every_capture_asks_the_runner_to_redact_literals(self):
        seen: list[Any] = []

        def capture(source, *args, **kwargs):
            seen.append(kwargs.get("redact_literals", "not passed"))
            return ok_capture(source.label)

        subject, _ = session()
        with mock.patch(CAPTURE, side_effect=capture):
            await subject.compare()

        assert seen == [True] * len(LABELS), seen


class TestRunDetails:
    """The verbose stream the run dialog is built from.

    Separate from ``on_progress`` on purpose: a capture reports ten steps, and sending those down
    the per-source channel would grow the Run tab's list by a row per object kind.
    """

    def built(self):
        from db_schema_diff.gui.session import RunDetail

        details: list[RunDetail] = []
        built = Session(CONFIG, store(), on_progress=[].append, on_detail=details.append)
        return built, details

    @pytest.mark.asyncio
    async def test_each_capture_step_is_reported_with_what_it_found(self, monkeypatch):
        from db_schema_diff.gui import session as session_module
        from db_schema_diff.gui.session import CAPTURE_PHASE

        def fake_capture(source, dsn, *, observer=None, **kwargs):
            observer("tables", 7)
            observer("columns", 41)
            return ok_capture(source.label)

        monkeypatch.setattr(session_module.runner, "capture", fake_capture)
        built, details = self.built()
        await built.compare()
        await asyncio.sleep(0.05)  # the reports are queued on the loop, not run on the spot

        steps = [(d.source, d.step, d.count) for d in details if d.phase == CAPTURE_PHASE]
        assert ("prod", "tables", 7) in steps
        assert ("prod", "columns", 41) in steps
        assert {label for label, _, _ in steps} == {"prod", "qa", "dev"}

    @pytest.mark.asyncio
    async def test_the_steps_reach_the_window_on_the_loop_thread(self, monkeypatch):
        """Slint values are pyo3 ``unsendable``: touching one from a worker aborts the process
        rather than raising. A capture runs in an executor, so every step is a cross-thread call."""
        from db_schema_diff.gui import session as session_module

        loop_thread = threading.get_ident()
        seen: list[int] = []

        def fake_capture(source, dsn, *, observer=None, **kwargs):
            assert threading.get_ident() != loop_thread, "the capture should be off the loop"
            observer("tables", 1)
            return ok_capture(source.label)

        monkeypatch.setattr(session_module.runner, "capture", fake_capture)
        built = Session(CONFIG, store(), on_detail=lambda _: seen.append(threading.get_ident()))
        await built.compare()
        await asyncio.sleep(0.05)

        assert seen, "no step was ever reported"
        assert set(seen) == {loop_thread}, "a step reached the window from a worker thread"

    @pytest.mark.asyncio
    async def test_the_diff_and_the_report_are_phases_of_their_own(self, monkeypatch):
        """Both are invisible otherwise: a large comparison looks finished when the last source is
        captured, and then sits there."""
        from db_schema_diff.gui import session as session_module
        from db_schema_diff.gui.session import COMPARE_PHASE, REPORT_PHASE

        monkeypatch.setattr(session_module.runner, "capture", succeed)
        built, details = self.built()
        await built.compare()

        phases = [d.phase for d in details]
        assert COMPARE_PHASE in phases
        assert REPORT_PHASE in phases
        assert phases.index(COMPARE_PHASE) < phases.index(REPORT_PHASE)

    @pytest.mark.asyncio
    async def test_a_consumer_that_raises_does_not_fail_the_comparison(self, monkeypatch):
        """Same contract as ``on_progress``: a broken progress display is not a failed run."""
        from db_schema_diff.gui import session as session_module

        monkeypatch.setattr(session_module.runner, "capture", succeed)

        def explode(_):
            raise RuntimeError("the dialog is broken")

        built = Session(CONFIG, store(), on_detail=explode)
        report = await built.compare()
        assert report is not None

    @pytest.mark.asyncio
    async def test_no_detail_consumer_is_fine(self, monkeypatch):
        from db_schema_diff.gui import session as session_module

        monkeypatch.setattr(session_module.runner, "capture", succeed)
        built, _ = session()
        assert await built.compare() is not None


class TestTunnelStepsCrossThreadsSafely:
    """Slint values are pyo3 `unsendable`: touching one from another thread does not raise, it
    aborts the process. paramiko is synchronous, so the probe runs in an executor — which makes
    every step report a cross-thread call that has to be marshalled back to the loop.
    """

    @pytest.mark.asyncio
    async def test_the_observer_is_called_on_the_loop_thread(self, monkeypatch):
        import threading

        from db_schema_diff.gui import session as session_module

        loop_thread = threading.get_ident()
        seen: list[int] = []

        def fake_check(source, dsn, *, ssh_passphrase=None, observer=None):
            # Runs in the executor, as the real one does.
            assert threading.get_ident() != loop_thread, "the probe should be off the loop"
            observer("gateway", "ok", "")
            return TunnelStatus(label=source.label, ok=True, detail="fine")

        monkeypatch.setattr(session_module.runner, "check_tunnel", fake_check)

        built, _ = session()
        await built.check_tunnel("qa", lambda *_: seen.append(threading.get_ident()))
        # A step is *queued* on the loop, not run on the spot, so it can land after the probe has
        # returned. Awaiting the probe is not awaiting its reports; the loop has to turn.
        await asyncio.sleep(0.05)

        assert seen, "the observer was never called"
        assert set(seen) == {loop_thread}, "a step reached the window from a worker thread"
