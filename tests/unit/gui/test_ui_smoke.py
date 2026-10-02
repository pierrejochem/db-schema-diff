"""Every .slint file compiles, and every property and callback the Python layer binds exists.

A renamed property is otherwise an AttributeError the first time somebody opens that tab. Slint
instantiates headlessly, so this needs no display. The GUI is an optional extra: without ``slint``
the whole module skips, so the 3.11 CLI suite stays green.
"""

from __future__ import annotations

import contextlib
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
        "run_tab.slint",
        "results_tab.slint",
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
    assert text.count("#d1fadf") == 1
    assert text.count("#027a48") == 1


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


def test_the_heading_reaches_the_tab(window):
    # No read-back of the tab's own text exists, so check the markup binds it.
    source = (UI / "main.slint").read_text()
    assert "selected-heading: root.selected-heading;" in source
    assert "root.selected-heading" in (UI / "results_tab.slint").read_text()


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
    pane = source[source.index('title: "Detail"') :]
    assert pane.count("root.diff-colour(line.kind)") == 2
    assert "#" not in "".join(
        line.split("//")[0] for line in pane.splitlines() if "diff-colour" not in line
    ).replace("#344054", "")


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
    assert height == 240
