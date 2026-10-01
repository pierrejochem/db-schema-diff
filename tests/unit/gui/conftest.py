"""One policy for the whole GUI package: the cyclic collector runs on the main thread only.

Slint's Python values are pyo3 ``unsendable`` classes. Freeing one on a thread other than the one
that created it does not raise — it aborts the process:

    thread '<unnamed>' panicked: slint_python::value::PyStruct is unsendable,
    but sent to another thread

The cyclic collector runs on whichever thread happens to cross its allocation threshold, and these
tests run worker threads on purpose (every capture does, and the session's cancellation tests hold
them there). A collection landing on a capture thread while any window's structs are garbage kills
the run, in a different test each time and with no Python traceback — the flakiest possible
failure.

So the collector is turned off around each test and run here, between tests, on the thread that
made those values. Reference counting is untouched, explicit ``gc.collect()`` still works (several
session tests rely on it), and the application does exactly the same thing in
``gui.app.run``/``gui.app._show`` for exactly the same reason.
"""

from __future__ import annotations

import gc
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def collect_on_the_thread_that_allocated() -> Iterator[None]:
    enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        # Drain here, where it is safe, so the next test starts with nothing collectable.
        gc.collect()
        if enabled:
            gc.enable()
