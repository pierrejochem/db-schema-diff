"""Two policies for the whole GUI package: where its home directory is, and when the collector runs.

The application keeps its one configuration in ``~/.db_schema_comparer`` and reads it back when
it starts. A test that reads or writes the home directory of whoever is running it is a test that
has already failed — it would open their real configuration, or leave files behind — so every test
in this package is pointed at a temporary directory instead.

The rest of this file is about the collector.

One policy for the whole GUI package: the cyclic collector runs on the main thread only.

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
def _own_home(tmp_path_factory, monkeypatch):
    """Point the application's home directory at a temporary one, for every test in this package."""
    from db_schema_comparer.gui import home

    monkeypatch.setenv(home.HOME_VARIABLE, str(tmp_path_factory.mktemp("db-home")))


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


def _model(*findings, ignored=()):
    return _model_of({"qa": (findings, ignored)})


def _model_of(targets):
    """A report with one target per ``{label: (findings, ignored)}`` entry."""
    from db_schema_comparer.diff.model import ComparisonReport, TargetDiff
    from db_schema_comparer.gui.results_vm import ResultsModel
    from tests.support.builders import source

    built = tuple(
        TargetDiff(
            master_label="prod",
            target_label=label,
            master_source=source("prod"),
            target_source=source(label),
            findings=tuple(findings),
            ignored=tuple(ignored),
        )
        for label, (findings, ignored) in targets.items()
    )
    report = ComparisonReport(
        name="t",
        master_label="prod",
        targets=built,
        notes=(),
        generated_at="2026-01-15T09:30:00+00:00",
        tool_version="0.1.0",
        fail_on="error",
        probe_failed=False,
    )
    return ResultsModel(report)


def differing(key, *deltas):
    from db_schema_comparer.diff.model import ObjectFinding, ObjectStatus
    from db_schema_comparer.diff.severity import Severity

    return ObjectFinding(
        key=key, status=ObjectStatus.DIFFERS, severity=Severity.ERROR, deltas=tuple(deltas)
    )


def delta(attribute, master, target, **extra):
    from db_schema_comparer.diff.model import AttributeDelta
    from db_schema_comparer.diff.severity import Severity

    return AttributeDelta(
        attribute=attribute,
        master_value=master,
        target_value=target,
        severity=Severity.ERROR,
        **extra,
    )


def view_key(name="v_open"):
    from db_schema_comparer.model.keys import ObjectKey
    from db_schema_comparer.model.kinds import ObjectKind

    return ObjectKey(ObjectKind.VIEW, "public", name)


@pytest.fixture
def model_with_a_changed_view():
    return _model(
        differing(
            view_key(),
            delta("view.definition", "SELECT a,\n b\nFROM t", "SELECT a,\n c\nFROM t", body=True),
        )
    )


@pytest.fixture
def model_with_an_added_column():
    from db_schema_comparer.diff.model import ObjectFinding, ObjectStatus
    from db_schema_comparer.diff.severity import Severity
    from db_schema_comparer.model.keys import column_key

    return _model(
        ObjectFinding(
            key=column_key("public", "t", "c"),
            status=ObjectStatus.DIFFERS,
            severity=Severity.ERROR,
            deltas=(delta("column.default", None, "0"),),
        )
    )


@pytest.fixture
def model_with_a_changed_column_type():
    from db_schema_comparer.model.keys import column_key

    return _model(
        differing(column_key("public", "t", "c"), delta("column.data_type", "varchar(40)", "text"))
    )


@pytest.fixture
def model_with_two_findings():
    return _model(
        differing(view_key("first"), delta("view.definition", "a\nb", "a\nc", body=True)),
        differing(view_key("second"), delta("view.definition", "x\ny", "x\nz", body=True)),
    )


def absent(key, status):
    """A finding with no deltas: an object that exists on one side only."""
    from db_schema_comparer.diff.model import ObjectFinding, ObjectStatus
    from db_schema_comparer.diff.severity import Severity

    return ObjectFinding(
        key=key,
        status=ObjectStatus(status),
        severity=Severity.ERROR if status == "missing_in_target" else Severity.WARNING,
    )


@pytest.fixture
def model_with_a_missing_and_an_extra_object():
    """The commonest findings there are, and the ones that carry no attribute deltas."""
    from db_schema_comparer.model.keys import column_key, table_key

    return _model(
        absent(view_key("v_gone"), "missing_in_target"),
        absent(table_key("public", "tmp_debug"), "extra_in_target"),
        absent(column_key("public", "tmp_debug", "note"), "extra_in_target"),
    )
