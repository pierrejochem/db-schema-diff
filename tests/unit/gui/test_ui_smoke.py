"""Every .slint file compiles, and every property and callback the Python layer binds exists.

A renamed property is otherwise an AttributeError the first time somebody opens that tab. Slint
instantiates headlessly, so this needs no display. The GUI is an optional extra: without ``slint``
the whole module skips, so the 3.11 CLI suite stays green.
"""

from __future__ import annotations

import pathlib

import pytest

slint = pytest.importorskip("slint", reason="the GUI extra is not installed")

UI = pathlib.Path(__file__).resolve().parents[3] / "src/cumo_schema_comparer/gui/ui"


def slint_files() -> list[pathlib.Path]:
    return sorted(UI.glob("*.slint"))


@pytest.fixture
def window():
    # Function scoped: tests mutate properties and must not see each other's values.
    return slint.load_file(str(UI / "main.slint")).MainWindow()


# (python name, a value of the declared type different from the default, i.e. what Task 11 sets)
PROPERTIES = [
    ("config_name", "cumo-invoicing"),
    ("config_path", "/work/config.yaml"),
    ("dirty", True),
    ("status_message", "saved"),
    ("status_is_error", True),
    ("exclude_schemas_text", "quartz, audit_archive"),
    ("connect_timeout_seconds", 17),
    ("statement_timeout_seconds", 91),
    ("lock_timeout_seconds", 5),
    ("parallel", False),
    ("max_workers", 9),
    ("fail_on", "never"),
    ("ignore_column_order", True),
    ("include_owners", True),
    ("include_comments", True),
    ("include_grants", True),
]

# name -> argument tuple of the declared types
CALLBACKS = {
    "source_changed": ("prod", "dsn_env", "PROD_DSN"),
    "add_target": (),
    "remove_target": ("qa",),
    "check_connection": ("qa",),
    "check_all": (),
    "validate_config": (),
    "save_config": (),
    "load_config": (),
}

SOURCE_ROW_FIELDS = [
    "label",
    "dsn_env",
    "host",
    "database",
    "schemas",
    "schema_map",
    "liquibase_schema",
    "liquibase_table",
    "is_master",
    "credential_source",
    "connection_status",
    "connection_ok",
    "checked",
]


def make_row(**overrides):
    row = {
        "label": "qa",
        "dsn_env": "QA_DSN",
        "host": "",
        "database": "",
        "schemas": "",
        "schema_map": "",
        "liquibase_schema": "",
        "liquibase_table": "",
        "is_master": False,
        "credential_source": "unset",
        "connection_status": "",
        "connection_ok": False,
        "checked": False,
    }
    row.update(overrides)
    return row


def test_there_are_slint_files_to_check():
    assert slint_files(), "the smoke test would pass vacuously with no .slint files"
    assert {p.name for p in slint_files()} >= {"main.slint", "config_tab.slint", "widgets.slint"}


@pytest.mark.parametrize("path", slint_files(), ids=lambda p: p.name)
def test_every_slint_file_compiles(path):
    # A syntax error in an unopened tab would otherwise surface only at run time.
    slint.load_file(str(path))


def test_the_main_window_instantiates(window):
    assert window is not None


@pytest.mark.parametrize(("name", "value"), PROPERTIES, ids=[p[0] for p in PROPERTIES])
def test_every_property_exists_and_round_trips(window, name, value):
    # Read BEFORE writing. SlintClassWrapper lets setattr on an undeclared name succeed by
    # shadowing it in the instance dict, so a write-then-read passes for a typo'd property too.
    # Only a declared property can be read before it has been set.
    getattr(window, name)
    setattr(window, name, value)
    assert getattr(window, name) == value


def test_the_property_list_has_one_entry_per_options_field():
    from cumo_schema_comparer.config.model import Options

    names = {n for n, _ in PROPERTIES}
    assert set(Options.model_fields) <= names, set(Options.model_fields) - names


def test_exclude_schemas_is_a_readable_string_list(window):
    # Read-first, as above: the list property itself, not only exclude_schemas_text.
    assert list(window.exclude_schemas) == []
    window.exclude_schemas = slint.ListModel(["quartz", "audit"])
    assert list(window.exclude_schemas) == ["quartz", "audit"]


@pytest.mark.parametrize(("name", "args"), CALLBACKS.items(), ids=list(CALLBACKS))
def test_every_callback_is_assigned_and_invoked_by_calling_it(window, name, args):
    # Read-first: assigning to an undeclared callback name silently succeeds, so prove it is
    # declared (an undeclared name raises AttributeError on read) and is callable.
    assert callable(getattr(window, name))
    seen = []
    setattr(window, name, lambda *a: seen.append(a))
    getattr(window, name)(*args)  # no invoke_ prefix
    assert seen == [args]
    assert not hasattr(window, f"invoke_{name}")


def test_source_row_declares_exactly_the_expected_fields():
    """A field dropped from the struct would otherwise be a silently absent dict key."""
    declared = {
        k.replace("-", "_") for k in dict(slint.load_file(str(UI / "main.slint")).SourceRow())
    }
    assert declared == set(SOURCE_ROW_FIELDS)


def test_a_source_row_round_trips_every_field(window):
    """The struct field names must match what the view model produces."""
    sent = make_row(
        label="prod",
        dsn_env="PROD_DSN",
        host="db-prod",
        database="invoicing",
        schemas="cumo-invoicing, public",
        schema_map="a=b",
        liquibase_schema="lb",
        liquibase_table="DATABASECHANGELOG",
        is_master=True,
        credential_source="environment",
        connection_status="ok",
        connection_ok=True,
        checked=True,
    )
    assert set(sent) == set(SOURCE_ROW_FIELDS)
    window.sources = slint.ListModel([sent])
    row = window.sources[0]
    assert {k: row[k] for k in SOURCE_ROW_FIELDS} == sent


def test_a_row_can_be_updated_by_assignment(window):
    """Connection status updates rows in place rather than rebuilding the model."""
    model = slint.ListModel([make_row()])
    window.sources = model
    model[0] = make_row(connection_status="PostgreSQL 15.19", connection_ok=True)
    assert window.sources[0]["connection_ok"] is True
    assert window.sources[0]["connection_status"] == "PostgreSQL 15.19"


def test_a_row_missing_a_field_is_not_defaulted_or_rejected(window):
    """Slint neither rejects nor defaults a partial dict; send every field."""
    row = make_row()
    del row["checked"]
    window.sources = slint.ListModel([row])
    assert "checked" not in window.sources[0]
