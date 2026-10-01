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
    "rule_changed": ("quartz-runtime", "action", "warn"),
    "add_rule": (),
    "remove_rule": ("quartz-runtime",),
    "save_ignores": (),
    "store_credential": ("PROD_DSN", "a-secret"),
    "forget_credential": ("PROD_DSN",),
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
    assert {p.name for p in slint_files()} >= {
        "main.slint",
        "config_tab.slint",
        "widgets.slint",
        "ignores_tab.slint",
        "credentials_tab.slint",
    }


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


RULE_ROW_FIELDS = [
    "id",
    "reason",
    "kinds",
    "names",
    "attributes",
    "targets",
    "statuses",
    "action",
    "read_only",
]
CREDENTIAL_ROW_FIELDS = ["env_name", "used_by", "source", "summary", "can_store"]


def make_rule(**overrides):
    row = {
        "id": "quartz-runtime",
        "reason": "runtime state",
        "kinds": "table, index",
        "names": "quartz.*, *.qrtz_*",
        "attributes": "",
        "targets": "",
        "statuses": "extra_in_target",
        "action": "ignore",
        "read_only": False,
    }
    row.update(overrides)
    return row


def make_credential(**overrides):
    row = {
        "env_name": "PROD_DSN",
        "used_by": "prod",
        "source": "keychain",
        "summary": "dbname=invoicing, host=db-prod, port=5432, user=cumo",
        "can_store": True,
    }
    row.update(overrides)
    return row


# (python name, a value of the declared type different from the default)
TAB_PROPERTIES = [
    ("case_insensitive_globs", True),
    ("keychain_available", True),
    ("keychain_problem", "no keyring backend"),
]
TAB_LISTS = ["rules", "default_rules", "credentials"]


@pytest.mark.parametrize(("name", "value"), TAB_PROPERTIES, ids=[p[0] for p in TAB_PROPERTIES])
def test_every_tab_scalar_property_exists_and_round_trips(window, name, value):
    before = getattr(window, name)  # read first: raises AttributeError if undeclared
    assert before != value
    setattr(window, name, value)
    assert getattr(window, name) == value


@pytest.mark.parametrize("name", TAB_LISTS)
def test_every_tab_list_property_exists_and_starts_empty(window, name):
    assert list(getattr(window, name)) == []


def declared_fields(struct_name):
    loaded = slint.load_file(str(UI / "main.slint"))
    return {k.replace("-", "_") for k in dict(getattr(loaded, struct_name)())}


def test_rule_row_declares_exactly_the_expected_fields():
    assert declared_fields("RuleRow") == set(RULE_ROW_FIELDS)


def test_credential_row_declares_exactly_the_expected_fields():
    # By construction there is no field that could hold a connection string or a secret.
    assert declared_fields("CredentialRow") == set(CREDENTIAL_ROW_FIELDS)


def test_a_rule_row_round_trips_every_field(window):
    sent = make_rule(read_only=True)
    assert set(sent) == set(RULE_ROW_FIELDS)
    window.rules = slint.ListModel([sent])
    window.default_rules = slint.ListModel([sent])
    for model in (window.rules, window.default_rules):
        row = model[0]
        assert {k: row[k] for k in RULE_ROW_FIELDS} == sent


def test_a_credential_row_round_trips_every_field(window):
    sent = make_credential()
    assert set(sent) == set(CREDENTIAL_ROW_FIELDS)
    window.credentials = slint.ListModel([sent])
    row = window.credentials[0]
    assert {k: row[k] for k in CREDENTIAL_ROW_FIELDS} == sent
    assert "password" not in str(row)


def test_a_credential_row_updates_by_assignment(window):
    model = slint.ListModel([make_credential(source="unset", summary="")])
    window.credentials = model
    model[0] = make_credential(source="environment")
    assert window.credentials[0]["source"] == "environment"


def test_a_stored_secret_reaches_no_property_on_the_window(window):
    """Drive the real chain: row handler -> tab -> main.slint forwarding -> window callback.

    The probe goes on the window's outward callback, which has no .slint handler to replace. A
    Python-assigned handler on a forwarding callback would replace the code under test.
    """
    secret = "postgresql://cumo:S3cr3t-Distinct-Pw@db-prod:5432/invoicing"
    window.credentials = slint.ListModel([make_credential()])
    window.rules = slint.ListModel([make_rule()])
    seen = []
    window.store_credential = lambda env, value: seen.append((env, value))
    window.drive_store_credential("PROD_DSN", secret)
    assert seen == [("PROD_DSN", secret)], "the secret must reach the outward callback intact"

    names = [
        n
        for n in dir(window)
        if not n.startswith("_") and n not in {"run", "show", "hide"} and n not in CALLBACKS
    ]
    assert {"credentials", "status_message", "keychain_problem", "config_name"} <= set(names)
    for name in names:
        value = getattr(window, name)
        if callable(value):
            continue  # public functions: probes and the driver
        iterable = hasattr(value, "__iter__") and not isinstance(value, str)
        rendered = repr(list(value)) if iterable else repr(value)
        assert "S3cr3t" not in rendered, name


def test_the_window_wires_the_models_into_the_tabs(window):
    # Read the tabs' own row counts, not the window's properties: `rules: []` in main.slint
    # leaves window.rules full and the tab empty.
    assert window.ignores_rule_count() == 0
    assert window.ignores_default_rule_count() == 0
    assert window.credential_row_count() == 0
    window.rules = slint.ListModel([make_rule(), make_rule(id="b")])
    window.default_rules = slint.ListModel([make_rule(id="d", read_only=True)])
    window.credentials = slint.ListModel([make_credential()])
    assert window.ignores_rule_count() == 2
    assert window.ignores_default_rule_count() == 1
    assert window.credential_row_count() == 1


@pytest.fixture
def rule_line():
    harness = pathlib.Path(__file__).resolve().parent / "harness" / "rule_line.slint"
    return slint.load_file(str(harness)).RuleLineHarness()


def test_an_editable_rule_accepts_input_everywhere(rule_line):
    rule_line.data = make_rule(read_only=False)
    assert rule_line.editable_count == 9


def test_a_bundled_default_accepts_input_nowhere(rule_line):
    """The only thing stopping a user editing rules they do not own."""
    rule_line.data = make_rule(read_only=True)
    assert rule_line.editable_count == 0


def test_the_action_box_offers_exactly_what_the_view_model_accepts(rule_line):
    from cumo_schema_comparer.gui.ignores_vm import RULE_ACTIONS

    assert list(rule_line.action_choices) == ["ignore", "warn", "info"]
    assert set(rule_line.action_choices) == set(RULE_ACTIONS)


def test_credentials_tab_masks_the_password_field_and_never_logs():
    text = (UI / "credentials_tab.slint").read_text()
    assert "input-type: password" in text
    for path in slint_files():
        assert "debug(" not in path.read_text(), path.name
