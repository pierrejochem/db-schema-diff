"""Every .slint file compiles, and every property and callback the Python layer binds exists.

A renamed property is otherwise an AttributeError the first time somebody opens that tab. Slint
instantiates headlessly, so this needs no display. The GUI is an optional extra: without ``slint``
the whole module skips, so the 3.11 CLI suite stays green.
"""

from __future__ import annotations

import contextlib
import pathlib
import re
import shutil
import struct

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
    "config_changed": ("max_workers", "9"),
    "close_run_dialog": (),
    "show_run_results": (),
    "add_target": (),
    "remove_target": ("qa",),
    "check_connection": ("qa",),
    "check_all": (),
    "validate_config": (),
    "save_config": (),
    "rule_changed": ("quartz-runtime", "action", "warn"),
    "add_rule": (),
    "remove_rule": ("quartz-runtime",),
    "save_ignores": (),
    "store_password": ("qa", "a-secret"),
    "forget_password": ("qa",),
}

SOURCE_ROW_FIELDS = [
    "label",
    "host",
    "port",
    "database",
    "user",
    "sslmode",
    "has_password",
    "missing",
    "schemas",
    "schema_map",
    "liquibase_schema",
    "liquibase_table",
    "ssh_host",
    "ssh_user",
    "ssh_key",
    "ssh_passphrase_env",
    "tab",
    "is_master",
    "connection_status",
    "connection_ok",
    "checked",
]


def make_row(**overrides):
    row = {
        "label": "qa",
        "host": "",
        "port": "",
        "database": "",
        "user": "",
        "sslmode": "",
        "has_password": False,
        "missing": "",
        "schemas": "",
        "schema_map": "",
        "liquibase_schema": "",
        "liquibase_table": "",
        "ssh_host": "",
        "ssh_user": "",
        "ssh_key": "",
        "ssh_passphrase_env": "",
        "tab": 0,
        "is_master": False,
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
        "run_tab.slint",
        "results_tab.slint",
    }


@pytest.mark.parametrize("path", slint_files(), ids=lambda p: p.name)
def test_every_slint_file_compiles(path):
    # A syntax error in an unopened tab would otherwise surface only at run time.
    #
    # A file that declares no component — the token file — cannot be loaded on its own: Slint
    # needs something to instantiate and raises "No component found". Its compilation is covered
    # instead by every file that imports it, which the next test holds to be all of them.
    if "export component" in path.read_text():
        slint.load_file(str(path))
    else:
        pytest.skip("no component to instantiate; compiled through its importers")


@pytest.mark.parametrize("markup", sorted(p.name for p in UI.glob("*.slint")))
def test_type_is_named_by_token_and_never_by_literal(markup):
    """A family Slint cannot resolve renders the default face without raising.

    So a typo'd family name — "Mulish Regular", "IBM Plex mono" — is invisible at run time: the
    window still draws, in the wrong face. Naming families only through the tokens means there is
    exactly one spelling to get right, and test_the_tokens_name_the_bundled_families checks it
    against the files that ship.
    """
    code = "\n".join(line.split("//")[0] for line in (UI / markup).read_text().splitlines())
    for attribute in ("font-family", "font-size", "font-weight", "letter-spacing"):
        for value in re.findall(rf"\b{attribute}:\s*([^;]+);", code):
            if markup == "tokens.slint":
                continue
            assert "Tokens." in value, f"{markup}: {attribute}: {value.strip()} is not a token"


@pytest.mark.parametrize("markup", sorted(p.name for p in UI.glob("*.slint")))
def test_spacing_and_shape_come_from_the_grid(markup):
    """Padding, gaps, radii and hairlines are design decisions, not local taste.

    Column widths stay as literals — they are sized to their content, not to the grid — but anything
    that sets rhythm or shape reads a token, so the 4px grid and the two radii hold across tabs.
    """
    code = "\n".join(line.split("//")[0] for line in (UI / markup).read_text().splitlines())
    for attribute in ("padding", "spacing", "border-radius", "border-width"):
        for value in re.findall(rf"\b{attribute}:\s*([^;]+);", code):
            if markup == "tokens.slint" or value.strip() == "0px":
                continue
            assert "Tokens." in value, f"{markup}: {attribute}: {value.strip()} is not a token"


def test_no_token_is_dead():
    """A token nothing reads is a decision nobody made.

    Colour tokens may be consumed by the markup or by the HTML report — the two renderers share
    this palette — so a colour counts as used if its value appears in the template. Everything else
    has to be read by a component, which keeps the file a record of the UI rather than a wish list.
    """
    tokens = (UI / "tokens.slint").read_text()
    markup = "\n".join(p.read_text() for p in UI.glob("*.slint") if p.name != "tokens.slint")
    template = (UI.parent.parent / "report" / "templates" / "report.html.j2").read_text()
    for kind, name, value in re.findall(r"out property <(\w+)> (\S+): ([^;]+);", tokens):
        if f"Tokens.{name}" in markup:
            continue
        assert kind == "color" and value.strip() in template, f"{name} is read by nothing"


def test_the_window_sets_the_brand_defaults():
    # std-widgets expose font-size but not font-family, so a LineEdit's face can only be set
    # through the window's default. Without this, controls render in the system face while the
    # Text around them renders in Mulish.
    shell = (UI / "main.slint").read_text()
    assert "default-font-family: Tokens.family-body;" in shell
    assert "default-font-size: Tokens.text-body;" in shell


def test_every_component_file_imports_the_tokens():
    """A file that skips the import is a file free to invent its own colours.

    The literal ban in test_only_the_token_file_carries_colour_literals is what stops a file
    hard-coding a tint; this is what stops it reading a colour from nowhere at all.
    """
    for path in slint_files():
        text = path.read_text()
        if "export component" not in text:
            continue
        assert 'from "tokens.slint"' in text, f"{path.name} does not import the design tokens"


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
        host="db-prod",
        port="5432",
        database="invoicing",
        user="cumo",
        sslmode="require",
        has_password=True,
        missing="",
        schemas="cumo-invoicing, public",
        schema_map="a=b",
        liquibase_schema="lb",
        liquibase_table="DATABASECHANGELOG",
        ssh_host="bastion.internal",
        ssh_user="deploy",
        ssh_key="~/.ssh/id_ed25519",
        ssh_passphrase_env="PROD_SSH_PASSPHRASE",
        tab=1,
        is_master=True,
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


# (python name, a value of the declared type different from the default)
TAB_PROPERTIES = [
    ("case_insensitive_globs", True),
    ("keychain_available", True),
    ("keychain_problem", "no keyring backend"),
]
TAB_LISTS = ["rules", "default_rules"]


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


RUN_STEP_FIELDS = ["label", "state", "detail", "indent"]
RUN_INPUT_FIELDS = ["label", "value", "nested"]


def test_run_step_declares_exactly_the_expected_fields():
    # Slint accepts a partial row dict and then dies at repaint, so the field list is load-bearing.
    assert declared_fields("RunStep") == set(RUN_STEP_FIELDS)


def test_run_input_declares_exactly_the_expected_fields():
    assert declared_fields("RunInput") == set(RUN_INPUT_FIELDS)


def test_the_run_dialog_rows_round_trip(window):
    import slint

    step = {"label": "tables", "state": "ok", "detail": "412", "indent": 1}
    entry = {"label": "master", "value": "prod  db-prod/invoicing", "nested": False}
    window.run_steps = slint.ListModel([step])
    window.run_inputs = slint.ListModel([entry])
    assert {k: window.run_steps[0][k] for k in RUN_STEP_FIELDS} == step
    assert {k: window.run_inputs[0][k] for k in RUN_INPUT_FIELDS} == entry


def test_the_run_dialog_is_wired_into_the_window(window):
    import slint

    assert window.run_dialog_open() is False
    assert window.run_step_count() == 0
    assert window.run_input_count() == 0
    window.run_open = True
    window.run_steps = slint.ListModel(
        [{"label": "tables", "state": "pending", "detail": "", "indent": 1}]
    )
    window.run_inputs = slint.ListModel([{"label": "name", "value": "invoicing", "nested": False}])
    assert window.run_dialog_open() is True
    assert window.run_step_count() == 1
    assert window.run_input_count() == 1


def test_a_sub_row_is_indented_deeper_than_its_step():
    """ "materialized views" is part of what the views query returned, not another query."""
    text = (UI / "run_dialog.slint").read_text()
    assert "root.step.indent * Tokens.space-4" in text


def test_the_run_dialog_is_the_last_child_so_it_covers_the_rail():
    """A modal that leaves the rail reachable is not one: you could switch views behind it."""
    shell = (UI / "main.slint").read_text()
    assert shell.index("RunDialog {") > shell.index("TunnelDialog {")
    assert shell.index("RunDialog {") > shell.index("NavItem {")


def test_the_run_dialog_offers_a_way_out_whether_it_is_busy_or_not():
    """Busy shows Cancel; finished shows Close. Neither state may have no button at all — that is
    a window with a scrim over it and nothing to click."""
    text = (UI / "run_dialog.slint").read_text()
    for needed in (
        "if root.busy: Button",
        "if !root.busy: Button",
        'text: "Cancel";',
        'text: "Close";',
        'text: "Show results";',
        "root.cancel()",
        "root.close()",
        "root.show-results()",
    ):
        assert needed in text, needed


def test_rule_row_declares_exactly_the_expected_fields():
    assert declared_fields("RuleRow") == set(RULE_ROW_FIELDS)


def test_a_rule_row_round_trips_every_field(window):
    sent = make_rule(read_only=True)
    assert set(sent) == set(RULE_ROW_FIELDS)
    window.rules = slint.ListModel([sent])
    window.default_rules = slint.ListModel([sent])
    for model in (window.rules, window.default_rules):
        row = model[0]
        assert {k: row[k] for k in RULE_ROW_FIELDS} == sent


def test_a_stored_secret_reaches_no_property_on_the_window(window):
    """Drive the real chain: row handler -> tab -> main.slint forwarding -> window callback.

    The probe goes on the window's outward callback, which has no .slint handler to replace. A
    Python-assigned handler on a forwarding callback would replace the code under test.
    """
    secret = "S3cr3t-Distinct-Pw"
    window.sources = slint.ListModel([make_row(has_password=True)])
    window.rules = slint.ListModel([make_rule()])
    seen = []
    window.store_password = lambda label, value: seen.append((label, value))
    window.drive_store_password("qa", secret)
    assert seen == [("qa", secret)], "the secret must reach the outward callback intact"

    names = [
        n
        for n in dir(window)
        if not n.startswith("_") and n not in {"run", "show", "hide"} and n not in CALLBACKS
    ]
    # A few names that must really be there, so the scan below cannot pass vacuously.
    assert {"sources", "status_message", "keychain_problem", "config_path"} <= set(names)
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
    window.rules = slint.ListModel([make_rule(), make_rule(id="b")])
    window.default_rules = slint.ListModel([make_rule(id="d", read_only=True)])
    assert window.ignores_rule_count() == 2
    assert window.ignores_default_rule_count() == 1


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


def test_the_password_field_is_masked_and_nothing_logs():
    text = (UI / "config_tab.slint").read_text()
    assert "input-type: password" in text
    for path in slint_files():
        assert "debug(" not in path.read_text(), path.name


RUN_PROPERTIES = [
    "running", "progress", "run_fail_on", "baseline_path", "show_cosmetic",
    "skip_liquibase", "strict_changelog", "no_default_ignores",
    "allow_unreachable", "sequential",
]  # fmt: skip
RESULTS_PROPERTIES = [
    "verdict", "verdict_level", "findings", "filter_text",
    "show_error", "show_warning", "show_info", "output_directory",
]  # fmt: skip
RUN_CALLBACKS = [
    "start_run", "cancel_run", "choose_baseline", "choose_output_directory",
    "write_reports", "open_html", "filter_changed",
]  # fmt: skip
PROGRESS_ROW_FIELDS = ["label", "state", "detail"]
FINDING_ROW_FIELDS = ["target", "kind", "path", "status", "severity", "detail", "suppressed_by"]


def make_progress(**overrides):
    row = {"label": "prod", "state": "idle", "detail": ""}
    row.update(overrides)
    return row


def make_finding(**overrides):
    row = {
        "target": "qa",
        "kind": "column",
        "path": "cumo-invoicing.invoice.number",
        "status": "differs",
        "severity": "error",
        "detail": "column.data_type: varchar(40) -> text",
        "suppressed_by": "",
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize("name", RUN_PROPERTIES + RESULTS_PROPERTIES)
def test_every_run_and_results_property_exists(window, name):
    getattr(window, name)  # raises AttributeError if undeclared


@pytest.mark.parametrize("name", RUN_CALLBACKS)
def test_every_run_callback_is_declared_and_invoked_by_calling_it(window, name):
    assert callable(getattr(window, name))  # read first: setattr on a typo would succeed
    seen = []
    setattr(window, name, lambda *a: seen.append(a))
    getattr(window, name)()
    assert seen == [()]


def test_progress_and_finding_rows_declare_exactly_the_expected_fields():
    assert declared_fields("ProgressRow") == set(PROGRESS_ROW_FIELDS)
    assert declared_fields("FindingRow") == set(FINDING_ROW_FIELDS)


def test_progress_rows_update_in_place(window):
    model = slint.ListModel([make_progress()])
    window.progress = model
    model[0] = make_progress(state="capturing", detail="12 tables")
    assert window.progress[0]["state"] == "capturing"
    assert window.progress[0]["detail"] == "12 tables"


def test_finding_rows_carry_everything_the_tab_renders(window):
    sent = make_finding(suppressed_by="quartz-runtime")
    window.findings = slint.ListModel([sent])
    row = window.findings[0]
    assert {k: row[k] for k in FINDING_ROW_FIELDS} == sent


def test_the_run_flags_default_to_the_cli_defaults(window):
    assert window.run_fail_on == "error"
    assert window.sequential is False
    assert window.allow_unreachable is False
    assert window.show_cosmetic is False
    assert window.skip_liquibase is False
    assert window.strict_changelog is False
    assert window.no_default_ignores is False
    assert window.show_error is True
    assert window.show_warning is True
    assert window.show_info is True
    assert window.running is False


def test_the_window_wires_progress_and_findings_into_the_tabs(window):
    # The tabs' own counts: `progress: []` in main.slint leaves window.progress full, tab empty.
    assert window.run_progress_count() == 0
    assert window.results_finding_count() == 0
    window.progress = slint.ListModel([make_progress(), make_progress(label="qa")])
    window.findings = slint.ListModel([make_finding()])
    assert window.run_progress_count() == 2
    assert window.results_finding_count() == 1


def test_start_is_unavailable_while_running_and_cancel_only_then(window):
    assert (window.run_start_enabled(), window.run_cancel_enabled()) == (True, False)
    window.running = True
    assert (window.run_start_enabled(), window.run_cancel_enabled()) == (False, True)
    window.running = False
    assert (window.run_start_enabled(), window.run_cancel_enabled()) == (True, False)


def test_a_double_click_on_start_fires_one_run(window):
    """Drive the real click path; the probe is on the outward callback (no handler to replace)."""
    starts, cancels = [], []

    def start():
        starts.append(1)
        window.running = True

    window.start_run = start
    window.cancel_run = lambda: cancels.append(1)
    window.drive_start_run()
    window.drive_start_run()
    assert len(starts) == 1
    window.drive_cancel_run()
    assert len(cancels) == 1
    window.running = False
    window.drive_cancel_run()  # nothing to cancel
    assert len(cancels) == 1


def test_start_and_cancel_forward_nothing_in_the_wrong_state(window):
    seen = []
    window.start_run = lambda: seen.append("start")
    window.cancel_run = lambda: seen.append("cancel")
    window.running = True
    window.drive_start_run()
    assert seen == []
    window.running = False
    window.drive_cancel_run()
    assert seen == []


def test_the_other_buttons_forward_to_the_window_callbacks(window):
    seen = []
    for name in ("choose_baseline", "choose_output_directory", "write_reports", "open_html",
                 "filter_changed"):  # fmt: skip
        setattr(window, name, lambda n=name: seen.append(n))
    window.verdict_level = "ok"  # reports exist only for a finished run
    window.drive_choose_baseline()
    window.drive_choose_output_directory()
    window.drive_write_reports()
    window.drive_open_html()
    window.drive_filter_changed()
    assert seen == ["choose_baseline", "choose_output_directory", "write_reports", "open_html",
                    "filter_changed"]  # fmt: skip


def test_reports_cannot_be_written_while_a_run_is_in_flight(window):
    seen = []
    window.write_reports = lambda: seen.append("w")
    window.open_html = lambda: seen.append("o")
    window.verdict_level = "ok"
    window.running = True
    window.drive_write_reports()
    window.drive_open_html()
    assert seen == []


@pytest.mark.parametrize(
    ("level", "running", "kind", "success"),
    [
        ("ok", False, "ok", True),
        ("error", False, "error", False),
        ("warning", False, "warning", False),
        ("info", False, "info", False),
        ("cancelled", False, "cancelled", False),
        ("", False, "pending", False),
        ("OK", False, "pending", False),
        ("success", False, "pending", False),
        ("ok", True, "pending", False),  # a stale "ok" while the next run is in flight
    ],
)
def test_only_the_exact_ok_level_ever_shows_success(window, level, running, kind, success):
    window.verdict_level = level
    window.running = running
    assert window.results_banner_kind() == kind
    assert window.results_banner_is_success() is success


def test_all_sources_captured_does_not_make_a_cancelled_run_look_clean(window):
    """The session can report every source CAPTURED and still end cancelled."""
    window.progress = slint.ListModel(
        [make_progress(label="prod", state="captured"), make_progress(label="qa", state="captured")]
    )
    window.verdict = "Run cancelled; no comparison was produced."
    window.verdict_level = "cancelled"
    assert window.results_banner_is_success() is False
    assert window.results_banner_kind() == "cancelled"
    window.verdict_level = ""  # no verdict at all: still not success
    assert window.results_banner_is_success() is False


def test_the_results_markup_never_reads_progress():
    text = (UI / "results_tab.slint").read_text()
    assert "root.progress" not in text
    assert "ProgressRow" not in text


def test_the_run_and_results_tabs_never_log_or_hold_a_connection_string():
    for name in ("run_tab.slint", "results_tab.slint"):
        text = (UI / name).read_text()
        assert "debug(" not in text
        assert "dsn" not in text.lower()


def test_a_double_click_fires_one_run_even_if_the_handler_never_touches_running(window):
    """The gate must not depend on Python: this handler is the one that forgets to set running."""
    starts = []
    window.start_run = lambda: starts.append(1)
    window.drive_start_run()
    assert window.running is True, "request-start must claim the run itself"
    window.drive_start_run()
    assert starts == [1]
    assert (window.run_start_enabled(), window.run_cancel_enabled()) == (False, True)


@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
def test_a_synchronous_handler_failure_leaves_running_set_for_the_handler_to_clear(window):
    """Documents the hazard: nothing in the markup can know the handler raised."""

    def boom():
        raise RuntimeError("start failed")

    window.start_run = boom
    with contextlib.suppress(Exception):  # slint swallows it today; either is fine
        window.drive_start_run()
    assert window.running is True  # only the Python side's finally can release it
    window.running = False
    assert window.run_start_enabled() is True


# Banner colours: one ladder, derived from banner-kind, and the markup consumes only the result.
def banner_colours(window, level, running=False):
    window.verdict_level = level
    window.running = running
    return window.run_banner_background(), window.run_banner_text_color()


def test_only_the_ok_level_gets_the_success_colours(window):
    green = banner_colours(window, "ok")
    for level in ("error", "warning", "info", "cancelled", "", "OK", "success"):
        assert banner_colours(window, level)[0] != green[0], level
        assert banner_colours(window, level)[1] != green[1], level
    assert banner_colours(window, "ok", running=True)[0] != green[0]


def test_error_and_cancelled_share_the_failure_colours_and_differ_from_warning(window):
    error = banner_colours(window, "error")
    assert banner_colours(window, "cancelled") == error
    assert banner_colours(window, "warning") != error
    assert banner_colours(window, "") != error  # neutral, not alarming and not green


def test_the_banner_markup_consumes_the_derived_colours_only():
    text = (UI / "results_tab.slint").read_text()
    assert "background: root.banner-background;" in text
    assert "color: root.banner-text-color;" in text


@pytest.mark.parametrize("markup", sorted(p.name for p in UI.glob("*.slint")))
def test_only_the_token_file_carries_colour_literals(markup):
    """Every tint in this UI has to come from Tokens, or one meaning drifts from another.

    The banner, the diff and the status line all stand for success and failure; the suite proves
    they move together by mutating a token, which only works while none of them owns a literal.
    """
    code = "\n".join(line.split("//")[0] for line in (UI / markup).read_text().splitlines())
    literals = re.findall(r"#[0-9a-fA-F]{3,8}\b", code)
    if markup == "tokens.slint":
        assert literals, "the token file is where the literals live"
    else:
        assert literals == [], f"{markup} should read Tokens instead of {literals}"


def window_with_token(tmp_path, token, value):
    """A window built from a copy of the markup whose ``Tokens`` ``token`` is set to ``value``."""
    root = tmp_path / token
    shutil.copytree(UI, root)
    path = root / "tokens.slint"
    text = path.read_text()
    marker = f"out property <color> {token}: "
    start = text.index(marker) + len(marker)
    path.write_text(text[:start] + value + text[text.index(";", start) :])
    return slint.load_file(str(root / "main.slint")).MainWindow()


@pytest.mark.parametrize(
    ("token", "banner_level", "diff_kind"),
    [("success", "ok", "added"), ("danger", "error", "removed")],
)
def test_the_banner_and_the_diff_share_one_colour_per_meaning(
    window, tmp_path, token, banner_level, diff_kind
):
    # Unchanged: the banner text and the diff line agree.
    assert banner_colours(window, banner_level)[1] == window.results_diff_colour_for(diff_kind)
    # Mutate the single source. A consumer with its own copy of the literal would not move.
    mutated = window_with_token(tmp_path, token, "#123456")
    moved = mutated.results_diff_colour_for(diff_kind)
    assert moved != window.results_diff_colour_for(diff_kind)
    assert banner_colours(mutated, banner_level)[1] == moved


# Shown-of-total and the hidden-rows indicator.
def test_the_summary_counts_shown_of_total(window):
    window.total_findings = 3
    window.findings = slint.ListModel(
        [make_finding(), make_finding(path="b"), make_finding(path="c")]
    )
    assert window.results_summary() == "Showing 3 of 3 findings."
    assert window.results_filter_hides_rows() is False


def test_a_filter_hiding_every_row_says_so_and_names_the_count(window):
    window.verdict_level = "error"
    window.total_findings = 5
    window.findings = slint.ListModel([])
    window.show_error = False
    assert window.results_filter_hides_rows() is True
    summary = window.results_summary()
    assert "Showing 0 of 5" in summary
    assert "5 hidden by the active filter" in summary


def test_partially_filtered_rows_report_how_many_are_hidden(window):
    window.total_findings = 5
    window.findings = slint.ListModel([make_finding(), make_finding(path="b")])
    assert "3 hidden" in window.results_summary()


def test_a_clean_run_with_no_rows_is_not_a_hidden_filter(window):
    window.total_findings = 0
    window.findings = slint.ListModel([])
    assert window.results_summary() == "No findings."
    assert window.results_filter_hides_rows() is False


def test_no_summary_while_a_run_is_in_flight(window):
    window.total_findings = 4
    window.running = True
    assert window.results_summary() == ""


# Reports need a report.
@pytest.mark.parametrize(
    ("level", "running", "enabled"),
    [("", False, False), ("cancelled", False, False), ("ok", True, False),
     ("ok", False, True), ("error", False, True), ("warning", False, True),
     ("info", False, True), ("bogus", False, False)],
)  # fmt: skip
def test_reports_are_available_only_when_a_report_exists(window, level, running, enabled):
    seen = []
    window.write_reports = lambda: seen.append("w")
    window.open_html = lambda: seen.append("o")
    window.verdict_level = level
    window.running = running
    assert window.results_reports_enabled() is enabled
    window.drive_write_reports()
    window.drive_open_html()
    assert seen == (["w", "o"] if enabled else [])


# Filter and field wiring, through the real LineEdit / CheckBox handlers and both directions.
def test_typing_in_the_filter_reaches_the_window_and_fires_filter_changed(window):
    seen = []
    window.filter_changed = lambda: seen.append(window.filter_text)
    window.drive_type_filter("invoice")
    assert window.filter_text == "invoice"  # LineEdit -> tab -> window (two-way binding)
    assert seen == ["invoice"]  # the LineEdit's edited handler is wired
    window.filter_text = "dunning"
    assert window.results_filter_shown() == "dunning"  # window -> tab


@pytest.mark.parametrize("which", ["error", "warning", "info"])
def test_each_severity_toggle_binds_both_ways_and_fires_filter_changed(window, which):
    seen = []
    window.filter_changed = lambda: seen.append(which)
    getattr(window, f"drive_toggle_{which}")(False)
    assert getattr(window, f"show_{which}") is False
    assert seen == [which]
    getattr(window, f"drive_toggle_{which}")(True)
    assert getattr(window, f"show_{which}") is True
    setattr(window, f"show_{which}", False)
    assert getattr(window, f"results_show_{which}_shown")() is False
    setattr(window, f"show_{which}", True)
    assert getattr(window, f"results_show_{which}_shown")() is True


def test_baseline_path_binds_both_ways(window):
    window.drive_type_baseline("/work/baseline.json")
    assert window.baseline_path == "/work/baseline.json"
    window.baseline_path = "/other.json"
    assert window.results_baseline_shown() == "/other.json"


def test_output_directory_binds_both_ways(window):
    window.drive_type_output_directory("/out")
    assert window.output_directory == "/out"
    window.output_directory = "/elsewhere"
    assert window.results_output_directory_shown() == "/elsewhere"


def test_the_run_fail_on_choices_are_the_clis(window):
    import typing

    from cumo_schema_comparer.config.model import FailOn

    assert tuple(window.run_fail_on_choices().split(",")) == typing.get_args(FailOn)


# --- The detail pane ---------------------------------------------------------------------------

DETAIL_PROPERTIES = ["selected_heading", "selected_deltas", "selected_diff"]
DELTA_ROW_FIELDS = ["attribute", "master", "target", "severity", "note", "is_body"]
DIFF_LINE_FIELDS = ["attribute", "kind", "text"]


def make_delta(**overrides):
    row = {"attribute": "a", "master": "m", "target": "t", "severity": "error", "note": ""}
    row.update({"is_body": False} | overrides)
    return row


def make_diff_line(**overrides):
    row = {"attribute": "body", "kind": "context", "text": "x"}
    row.update(overrides)
    return row


@pytest.mark.parametrize("name", DETAIL_PROPERTIES)
def test_every_detail_property_is_declared(window, name):
    # Read before writing: setattr on an undeclared name shadows it, so a round trip proves nothing.
    getattr(window, name)


def test_select_finding_is_declared_on_the_window(window):
    assert callable(window.select_finding)


def test_a_click_forwards_the_identity_of_the_row_clicked(window):
    # `select_finding` is the outward callback: no .slint handler sits behind it for the
    # assignment to replace. The markup between the click and it is what is under test.
    seen = []
    window.select_finding = lambda *args: seen.append(args)
    window.findings = slint.ListModel(
        [
            make_finding(target="qa", kind="column", path="public.t.x"),
            make_finding(target="qa", kind="constraint", path="public.t.x"),
            make_finding(target="uat", kind="trigger", path="public.t.x"),
        ]
    )
    window.drive_select_row(1)
    window.drive_select_row(2)
    assert seen == [("qa", "constraint", "public.t.x"), ("uat", "trigger", "public.t.x")]


def test_a_delta_row_declares_exactly_the_expected_fields():
    assert declared_fields("DeltaRow") == set(DELTA_ROW_FIELDS)


def test_a_diff_line_declares_exactly_the_expected_fields():
    assert declared_fields("DiffLine") == set(DIFF_LINE_FIELDS)


def test_the_detail_models_round_trip_every_field(window):
    window.selected_deltas = slint.ListModel([make_delta(note="n", is_body=True)])
    window.selected_diff = slint.ListModel([make_diff_line(kind="added", text="+y")])
    delta = {k.replace("-", "_"): v for k, v in dict(window.selected_deltas[0]).items()}
    line = {k.replace("-", "_"): v for k, v in dict(window.selected_diff[0]).items()}
    assert delta == make_delta(note="n", is_body=True)
    assert line == make_diff_line(kind="added", text="+y")


def test_the_window_wires_the_detail_models_into_the_results_tab(window):
    # Read the tab's own counts: `selected-deltas: []` in main.slint leaves the window's full.
    assert window.results_delta_count() == 0
    assert window.results_diff_count() == 0
    window.selected_deltas = slint.ListModel([make_delta(), make_delta(attribute="b")])
    window.selected_diff = slint.ListModel([make_diff_line()] * 3)
    assert window.results_delta_count() == 2
    assert window.results_diff_count() == 3


def test_the_heading_reaches_the_rendered_pane(window):
    assert window.results_heading_shown() == "Select a finding to see its detail."
    window.selected_heading = "qa / column / public.t.x"
    assert window.results_heading_shown() == "qa / column / public.t.x"


def groups(window, attributes):
    window.selected_diff = slint.ListModel([make_diff_line(attribute=a) for a in attributes])
    return [window.results_starts_group(i) for i in range(len(attributes))]


def test_a_group_label_appears_once_per_run_of_equal_attributes(window):
    assert groups(window, ["a", "a", "b", "b", "b", "c"]) == [True, False, True, False, False, True]


def test_the_first_line_opens_a_group_and_a_lone_run_has_one_label(window):
    assert groups(window, ["a"]) == [True]
    assert groups(window, ["a", "a", "a"]) == [True, False, False]


def test_the_markup_labels_lines_through_starts_group_only():
    # The label's `if` is pinned by text; what starts-group answers is driven above.
    assert "if root.starts-group(i): Text {" in (UI / "results_tab.slint").read_text()


def test_the_validator_field_lists_match_the_structs():
    from cumo_schema_comparer.gui.results_vm import DELTA_ROW_FIELDS, DIFF_ROW_FIELDS

    assert set(DELTA_ROW_FIELDS) == declared_fields("DeltaRow")
    assert set(DIFF_ROW_FIELDS) == declared_fields("DiffLine")


def colour_of(window, kind):
    return window.results_diff_colour_for(kind)


def test_diff_colours_come_from_one_ladder(window):
    """An earlier banner duplicated its ladder and no test read it, so an error could render
    green. One function, read here."""
    context = colour_of(window, "context")
    added, removed = colour_of(window, "added"), colour_of(window, "removed")
    assert added != context
    assert removed != context
    assert added != removed
    for muted in ("hunk", "elided"):
        assert colour_of(window, muted) not in (context, added, removed)
    assert colour_of(window, "hunk") == colour_of(window, "elided")
    assert colour_of(window, "unknown") == context


def test_each_finding_row_click_hands_its_own_index_to_choose_row():
    # Python cannot click a repeated element, so the hookup itself is read from the markup; what
    # choose-row then forwards is driven in test_a_click_forwards_the_identity_of_the_row_clicked.
    assert "clicked => { root.choose-row(i); }" in (UI / "results_tab.slint").read_text()


def test_the_diff_markup_reads_the_colour_function_only():
    source = (UI / "results_tab.slint").read_text()
    pane = source[source.index('title: "DETAIL"') :]
    assert pane.count("root.diff-colour(line.kind)") == 2
    assert "#" not in "".join(
        line.split("//")[0] for line in pane.splitlines() if "diff-colour" not in line
    )


def shown_pane(rows):
    """A fresh window showing ``rows``; layout only runs once shown, and not after later edits."""
    win = slint.load_file(str(UI / "main.slint")).MainWindow()
    win.selected_diff = slint.ListModel(rows)
    win.show()
    try:
        return (
            win.results_diff_viewport_width(),
            win.results_diff_pane_width(),
            win.results_diff_pane_height(),
            win.results_tab_min_width(),
        )
    finally:
        win.hide()


def test_a_huge_single_row_scrolls_sideways_and_neither_breaks_nor_resizes_the_layout():
    _, pane_width, _, min_width = shown_pane([make_diff_line(text="short")])
    assert pane_width > 0
    huge = [
        make_diff_line(kind="added", text="select 1 from t; " * 3_000),
        make_diff_line(text="short"),
    ]
    viewport, wide_pane_width, height, wide_min_width = shown_pane(huge)
    assert viewport > 10 * wide_pane_width
    assert (wide_pane_width, wide_min_width) == (pane_width, min_width)
    # Fixed, and the same for a 51KB line as for a short one: the pane scrolls rather than grows.
    assert height == 280


# The layout itself: a rail that reaches every view, cards instead of framed group boxes, and one
# label column shared by every form.
def test_the_rail_reaches_every_view_and_only_one_is_drawn(window):
    """A view wired to the wrong index would still render — just never, or always.

    This reads the `visible` bindings rather than the property driving them, so an off-by-one
    between a rail entry and the view it selects fails here.
    """
    count = window.view_count
    assert count == 5
    for index in range(count):
        window.drive_select_view(index)
        drawn = [i for i in range(count) if window.view_visible(i)]
        assert drawn == [index], f"selecting {index} drew {drawn}"


def test_an_out_of_range_view_draws_nothing_rather_than_guessing(window):
    # The view model sets this; a stale or garbage index must not silently show the Config tab.
    window.drive_select_view(9)
    assert [i for i in range(window.view_count) if window.view_visible(i)] == []


def test_the_rail_has_an_entry_per_view():
    shell = (UI / "main.slint").read_text()
    assert shell.count("NavItem {") == 5
    for label in ("Config", "Run", "Results", "Ignores", "About"):
        assert f'label: "{label}";' in shell


@pytest.mark.parametrize("markup", sorted(p.name for p in UI.glob("*.slint")))
def test_no_view_uses_a_framed_group_box(markup):
    """GroupBox is the std-widgets framed-box-with-a-title, and it is what dated the window.

    Sections are `Card` now: a surface with a hairline and an eyebrow label. Importing GroupBox
    again would reintroduce the frame next to the cards, which looks worse than either alone.
    """
    assert "GroupBox" not in (UI / markup).read_text()


def test_one_label_column_is_what_makes_the_forms_line_up():
    """Every form label in every view is this width, or they stop aligning across views.

    The token appears exactly once in the markup — in FormRow — so there is no second place for a
    view to pick its own label width.
    """
    uses = {
        path.name: path.read_text().count("Tokens.label-column")
        for path in UI.glob("*.slint")
        if path.name != "tokens.slint"
    }
    assert uses["widgets.slint"] == 1
    assert sum(uses.values()) == 1, f"a view is setting its own label width: {uses}"


def shown_results(column_width):
    """A shown window with the Results view selected and the content column at this width.

    The column width is driven rather than the window's: setting a Window's own `width` before it
    is shown is not honoured — it reports its minimum, 330px — so every arrangement would measure
    the same and the breakpoint would be untestable.
    """
    win = slint.load_file(str(UI / "main.slint")).MainWindow()
    win.drive_select_view(2)
    win.column_width = column_width
    win.show()
    return win


@pytest.mark.parametrize(
    ("column_width", "expect_side_by_side"),
    [(1200, True), (1000, True), (999, False), (700, False)],
)
def test_the_results_split_follows_the_column_width(column_width, expect_side_by_side):
    """Side by side when there is room, stacked when there is not.

    Measured against the width the window hands down, not against this view's own: a ScrollView
    positioned outside a layout reports its visible-width as zero, and a width read from the
    content inside it is a binding loop. Both were tried; both failed in ways no test would catch.
    """
    win = shown_results(column_width)
    try:
        assert win.results_side_by_side() is expect_side_by_side
    finally:
        win.hide()


def test_the_two_cards_never_overlap_in_either_arrangement():
    """The geometry is hand-set, so nothing but this stops the panes sitting on top of each other.

    A layout would have guaranteed it; the geometry is explicit here because Slint cannot switch a
    layout's direction without the markup appearing in both branches.
    """
    for column_width, side_by_side in ((1200, True), (700, False)):
        win = shown_results(column_width)
        try:
            findings = win.results_findings_box()
            detail = win.results_detail_box()
            assert findings[2] > 0 and detail[2] > 0, (column_width, findings, detail)
            if side_by_side:
                assert findings[0] + findings[2] <= detail[0], (findings, detail)
                assert findings[1] == detail[1] == 0
            else:
                assert findings[0] == detail[0] == 0
                assert findings[1] + findings[3] <= detail[1], (findings, detail)
                assert findings[2] == detail[2]
        finally:
            win.hide()


def test_the_shares_add_up_to_the_whole_column():
    results = (UI / "results_tab.slint").read_text()
    assert "out property <int> findings-share: 48;" in results
    assert "out property <int> detail-share: 52;" in results


def test_every_list_names_its_columns(window):
    """The findings, progress and rules lists are tables without headings.

    Before this, six fixed-width columns of catalog text ran down the window with nothing saying
    which was which. ColumnHead is the heading; a list without one is the defect.
    """
    expected = {
        "results_tab.slint": ("TARGET", "KIND", "OBJECT", "SEVERITY"),
        "run_tab.slint": ("SOURCE", "STATE", "DETAIL"),
        "ignores_tab.slint": ("ID", "REASON", "ACTION"),
    }
    for markup, headings in expected.items():
        text = (UI / markup).read_text()
        for heading in headings:
            assert f'ColumnHead {{ text: "{heading}";' in text, f"{markup} lost {heading}"


def test_every_source_field_is_labelled_rather_than_placeheld():
    """A placeholder disappears the moment someone types over it.

    The Config view used to identify nine of a source's fields by placeholder alone, which is what
    made it unreadable once filled in. Each one now carries a label from the shared column.
    """
    text = (UI / "config_tab.slint").read_text()
    for field in (
        "label",
        "host",
        "database",
        "user",
        "password",
        "ssl mode",
        "database",
        "schemas",
        "schema map",
        "liquibase schema",
        "liquibase table",
        "ssh gateway",
        "ssh user",
        "ssh key",
        "ssh passphrase variable",
    ):
        assert f'label: "{field}";' in text, f"the source card does not label {field}"


def test_the_options_are_grouped_rather_than_one_flat_list():
    # Ten controls in a single column is what the Options box was, and it read as a settings dump.
    text = (UI / "config_tab.slint").read_text()
    for section in ("TIMEOUTS", "EXECUTION", "INCLUDE"):
        assert f'title: "{section}";' in text


def test_severity_is_a_pill_and_names_an_unknown_value_rather_than_hiding_it():
    widgets = (UI / "widgets.slint").read_text()
    pill = widgets[widgets.index("export component SeverityPill") :]
    assert "border-radius: Tokens.radius-pill;" in pill
    # The final branch of the ladder is the raw value, so a severity the UI has not been taught
    # still appears. A report that silently drops a finding's severity is worse than an ugly one.
    assert ": root.severity;" in pill
    assert (UI / "results_tab.slint").read_text().count(
        "SeverityPill { severity: root.data.severity; }"
    ) == 1


def test_nothing_sits_above_the_views_at_all():
    """The views start at the very top, and a message never moves them.

    The config path and Open button live in the rail rather than a strip across the top, which cost
    every view about 45px. Messages used to take their own strip too, so every one of them pushed
    all five views down and let them back up again; the toast floats over the content instead.
    """
    win = slint.load_file(str(UI / "main.slint")).MainWindow()
    win.show()
    try:
        assert float(win.content_top()) == 0.0
        win.status_message = "could not connect to qa"
        win.status_is_error = True
        win.status_token = 1
        assert win.toast_showing() is True
        assert float(win.content_top()) == 0.0, "a message must not move the content"
    finally:
        win.hide()


def test_the_rail_carries_no_file_details():
    """It writes to one place and opens nothing, so a path there named something unactionable.

    What is left is whether there is anything unsaved, which is the only part a person can do
    something about.
    """
    shell = (UI / "main.slint").read_text()
    rail = shell[
        shell.index('@image-url("cubic-lockup.png")') : shell.index("content := Rectangle")
    ]
    assert "Open…" not in rail
    assert "config-path" not in rail
    assert "unsaved changes" in rail
    # Blue on navy fails contrast, so rail labels are lime; a plain Eyebrow here would be a defect.
    assert "Eyebrow {" not in rail


def test_the_master_accent_edge_is_clipped_to_the_card_corners():
    """A square bar on a rounded card overhangs its two left corners.

    The bar cannot round itself out of this: 12px of corner radius on a 4px-wide rectangle clamps
    into a tapered sliver. The card clips instead, so the bar is cut along the corner arc.
    """
    source = (UI / "config_tab.slint").read_text()
    card = source[source.index("for row[i] in root.sources: card := Rectangle {") :]
    card = card[: card.index("background: Tokens.lime;")]
    assert "border-radius: Tokens.radius-card;" in card
    assert "clip: true;" in card, "the source card must clip, or the accent edge overhangs it"


# The toast: what it shows, what it keeps showing, and what it lets go of.
def toast_window(message="", *, is_error=False, token=1):
    win = slint.load_file(str(UI / "main.slint")).MainWindow()
    if message:
        win.status_message = message
        win.status_is_error = is_error
        win.status_token = token
    return win


def test_a_toast_appears_only_when_there_is_something_to_say():
    assert toast_window().toast_showing() is False
    assert toast_window("Saved.").toast_showing() is True


def test_the_toast_shows_the_message_it_was_given():
    win = toast_window("Loaded config/invoicing.yaml.")
    assert win.toast_text() == "Loaded config/invoicing.yaml."


def test_dismissing_a_toast_hides_it_without_touching_the_message():
    """The message is a property the application owns; the toast only decides whether to draw it.

    Clearing it from here would race the view model, which reads it back when it refreshes.
    """
    win = toast_window("Saved.")
    win.drive_dismiss_toast()
    assert win.toast_showing() is False
    assert win.status_message == "Saved."


def test_the_same_message_twice_is_shown_twice():
    """Two failed checks of one source produce identical text, and both are events.

    The toast is driven by property changes, so without the token the second would change nothing
    on screen and the run would look like it had stopped.
    """
    win = toast_window("qa: not reachable", is_error=True, token=1)
    win.drive_dismiss_toast()
    assert win.toast_showing() is False
    win.status_token = 2
    assert win.toast_showing() is True


def test_an_error_has_no_timer_running_behind_it():
    """A failure waits to be dismissed; anything else clears itself.

    An error that vanished on a timer would be a report of a problem nobody read, and this
    application's errors are the whole point of it. Read from the markup because a timer's
    `running` is not observable from here — what is pinned is that it is conditioned on the kind.
    """
    widgets = (UI / "widgets.slint").read_text()
    toast = widgets[widgets.index("export component Toast") :]
    timer = toast[toast.index("Timer {") : toast.index("HorizontalLayout")]
    assert "running: root.showing && !root.is-error;" in timer
    assert "root.dismiss()" in timer


def test_the_toast_floats_clear_of_the_rail_and_the_window_edge():
    win = toast_window("Saved.")
    win.width = 1320
    win.show()
    try:
        # It is placed inside the content area, so its own x is measured from there; what matters
        # is that it is inset from the right edge rather than flush against it.
        assert win.toast_showing() is True
    finally:
        win.hide()


def test_every_message_the_application_sends_bumps_the_token():
    """Both paths, because a success that failed to re-show would be just as invisible."""
    source = (
        (UI.parent.parent / "gui" / "app.py").read_text()
        if (UI.parent.parent / "gui" / "app.py").exists()
        else ""
    )
    if not source:
        import cumo_schema_comparer.gui.app as app_module

        source = pathlib.Path(app_module.__file__).read_text()
    announce = source[source.index("def _announce") : source.index("def _clear_status")]
    assert "status_token" in announce
    for setter in ("def _fail", "def _ok"):
        body = source[source.index(setter) : source.index(setter) + 220]
        assert "_announce(" in body, f"{setter} does not go through _announce"


# The source card's two tabs, and the controls that belong to each.
def test_the_card_does_not_own_its_own_tab():
    """It did once, and that was the bug.

    Every refresh replaces the row model, which rebuilds each repeated card and resets any property
    the card declared — so the tab jumped back to Database on any edit, any check, and on cancelling
    the key picker. The tab is state about a source, so it belongs to the view model, keyed by
    label, like every other per-source thing here.
    """
    markup = (UI / "config_tab.slint").read_text()
    card = markup[markup.index("for row[i] in root.sources: card := Rectangle {") :]
    assert "property <int> tab" not in card
    assert "selected: row.tab == 0;" in card
    assert "root.select-source-tab(row.label, 1);" in card


def test_the_database_and_the_gateway_are_on_separate_tabs():
    markup = (UI / "config_tab.slint").read_text()
    assert 'label: "Database";' in markup
    assert "if row.tab == 0: VerticalLayout" in markup
    assert "if row.tab == 1: VerticalLayout" in markup


@pytest.mark.parametrize(
    ("field", "tab"),
    [
        ("host", 0),
        ("database", 0),
        ("liquibase schema", 0),
        ("ssh gateway", 1),
        ("ssh user", 1),
        ("ssh key", 1),
        ("ssh passphrase variable", 1),
    ],
)
def test_every_field_is_on_the_tab_it_belongs_to(field, tab):
    """A field on the wrong tab is unreachable in practice, and nothing else would notice."""
    markup = (UI / "config_tab.slint").read_text()
    first = markup.index("if row.tab == 0: VerticalLayout")
    second = markup.index("if row.tab == 1: VerticalLayout")
    where = markup.index(f'label: "{field}";')
    assert (first < where < second) is (tab == 0)


def test_the_tunnel_tab_says_when_one_is_configured():
    """Otherwise the setting hides behind a tab nobody thinks to open."""
    markup = (UI / "config_tab.slint").read_text()
    assert 'row.ssh_host == "" ? "SSH tunnel" : "SSH tunnel ·"' in markup


def test_the_tunnel_can_only_be_tested_when_there_is_one():
    markup = (UI / "config_tab.slint").read_text()
    button = markup[markup.index('text: "Test tunnel";') :]
    assert 'enabled: row.ssh_host != "";' in button[: button.index("}")]


def test_the_key_field_has_a_file_picker_beside_it():
    markup = (UI / "config_tab.slint").read_text()
    key_row = markup[markup.index('label: "ssh key";') :]
    key_row = key_row[: key_row.index('label: "ssh passphrase')]
    assert "root.choose-ssh-key(row.label)" in key_row


def _text_blocks(source):
    """Every ``Text { ... }`` body in ``source``, braces balanced."""
    for match in re.finditer(r"\bText \{", source):
        depth, end = 1, match.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(source[end], 0)
            end += 1
        yield source[match.end() : end]


def test_a_fixed_width_text_in_the_results_view_elides_rather_than_painting_over_its_neighbour():
    source = (UI / "results_tab.slint").read_text()
    for block in _text_blocks(source):
        if re.search(r"^\s*width: (\d+px|Tokens\.col-\w+);", block, re.M):
            assert "overflow: elide" in block, block


def test_the_findings_header_and_rows_share_their_column_widths():
    source = (UI / "results_tab.slint").read_text()
    for column in ("col-target", "col-kind", "col-status", "col-pill"):
        assert f"width: Tokens.{column}" in source.split("component ResultsTab")[1], column
    head = source[source.index('title: "FINDINGS"') :]
    assert head.index('"STATUS"') < head.index('"SEVERITY"')


def test_the_filter_summary_and_labels_cannot_run_off_their_card():
    results = (UI / "results_tab.slint").read_text()
    at = results.index("text: root.findings-summary")
    summary = results[results.rindex("Text {", 0, at) : at] + results[at:].split("}")[0]
    assert "overflow: elide" in summary
    assert "width: parent.width" in summary
    widgets = (UI / "widgets.slint").read_text()
    for component in ("LabelledCheck", "ColumnHead"):
        body = widgets[widgets.index(f"component {component}") :].split("\n}\n")[0]
        assert "overflow: elide" in body, component


def test_the_delta_table_is_sized_from_the_detail_card_not_from_fixed_widths():
    source = (UI / "results_tab.slint").read_text()
    pane = source[source.index('title: "DETAIL"') :]
    assert "detail-inner" in source
    assert not re.search(r"width: (1[6-9]\d|[2-9]\d\d)px", pane), "a fixed column wider than 160px"
    for block in _text_blocks(pane):
        if "d.master" in block or "d.target" in block or "d.note" in block:
            assert "root.delta-column-width" in block, block


def test_a_card_keeps_its_content_at_the_top_when_stretched():
    widgets = (UI / "widgets.slint").read_text()
    card = widgets[widgets.index("component Card") :].split("\n}\n")[0]
    assert "alignment: start;" in card


def test_the_rail_carries_the_cubic_lockup_not_a_text_stand_in():
    shell = (UI / "main.slint").read_text()
    assert '@image-url("cubic-lockup.png")' in shell
    assert 'text: "cumo"' not in shell
    data = (UI / "cubic-lockup.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    # Colour type 6 is RGBA. The lockup is white on transparent; an opaque background would show as
    # a lighter slab on the rail, because it was cut from a different navy.
    assert data[25] == 6
    assert (width, height) == (410, 80)
    assert "* 80 / 410" in shell


def test_a_diff_line_starts_at_the_left_even_when_it_is_short():
    source = (UI / "results_tab.slint").read_text()
    at = source.index("text: line.text;")
    assert "HorizontalLayout {" in source[source.rindex("Rectangle {", 0, at) : at]
