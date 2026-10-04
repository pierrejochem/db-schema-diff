"""Wiring: the window's callbacks reach the view models, and errors become messages.

Instantiated headlessly and driven by invoking callbacks, so this needs no display and no
database. Runs are driven for real — the markup's own button handlers, the real view models, the
real session — with only ``runner.capture`` and ``runner.check_connection`` standing in for a
server, because everything this module can get wrong lives between the window and the session.

The keychain is a fake. A real one would prompt on macOS and refuse on CI, and neither tells us
anything about the wiring.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

pytest.importorskip("slint", reason="the GUI extra is not installed")

from db_schema_diff.config.loader import load_config_files
from db_schema_diff.errors import ProbeError
from db_schema_diff.gui import app as app_module
from db_schema_diff.gui.app import Application
from db_schema_diff.gui.config_vm import ConfigDocument
from db_schema_diff.gui.errors import GuiError
from db_schema_diff.gui.session import Session
from db_schema_diff.runner import CaptureResult, ConnectionStatus, TunnelStatus
from tests.support.builders import col, inventory, table

CONFIG = textwrap.dedent(
    """
    version: 1
    name: invoicing
    master:
      label: prod
      dsn_env: PROD_DSN
    targets:
    - label: qa
      dsn_env: QA_DSN
    """
).lstrip()

SECRET = "hunter2"  # it must never appear anywhere the window can show
QA_DSN = f"postgresql://u:{SECRET}@qa/db"
PROD_DSN = f"postgresql://u:{SECRET}@prod/db"
#: Both sources resolve, so a run reaches the (stubbed) capture rather than failing on a
#: missing credential — and every assertion about what the window shows is made with a real
#: credential in play.
BOTH = {"PROD_DSN": PROD_DSN, "QA_DSN": QA_DSN}
CAPTURE = "db_schema_diff.gui.session.runner.capture"
CHECK = "db_schema_diff.gui.session.runner.check_connection"
BUILD_REPORT = "db_schema_diff.gui.session.runner.build_report"
#: For the tests that look at the dialog before the probe has answered. A bare ``Mock`` return
#: value would reach ``window.tunnel_ok`` once the loop drained and die there — a real status
#: keeps the failure in the test that asked for one.
UNFINISHED = TunnelStatus(label="qa", ok=False, detail="not asked")

SOURCE_ROW_FIELDS = {
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
}
RULE_ROW_FIELDS = {
    "id",
    "reason",
    "kinds",
    "names",
    "attributes",
    "targets",
    "statuses",
    "action",
    "read_only",
}
PROGRESS_ROW_FIELDS = {"label", "state", "detail"}
FINDING_ROW_FIELDS = {"target", "kind", "path", "status", "severity", "detail", "suppressed_by"}


class FakeKeychain:
    """An in-memory keyring backend."""

    def __init__(self, **entries: str) -> None:
        self.entries = dict(entries)

    def get_password(self, service: str, name: str) -> str | None:
        return self.entries.get(name)

    def set_password(self, service: str, name: str, value: str) -> None:
        self.entries[name] = value

    def delete_password(self, service: str, name: str) -> None:
        del self.entries[name]


class BrokenKeychain:
    """A backend that refuses, with a connection string in its complaint."""

    def get_password(self, service: str, name: str) -> str | None:
        raise RuntimeError(f"cannot reach the keychain while connected to {QA_DSN}")

    def set_password(self, service: str, name: str, value: str) -> None:
        raise RuntimeError("no")

    def delete_password(self, service: str, name: str) -> None:
        raise RuntimeError("no")


class EchoingKeychain:
    """A backend that quotes back the keyword string it refused.

    Not hypothetical: this is the one surface with no ``Dsn`` in scope to scrub against, so
    ``_sanitise`` is the only layer standing between a backend's complaint and two properties.
    """

    def __init__(self, keywords: str) -> None:
        self.keywords = keywords

    def get_password(self, service: str, name: str) -> str | None:
        raise RuntimeError(f"denied: could not open {self.keywords} for {name}")

    def set_password(self, service: str, name: str, value: str) -> None:
        raise RuntimeError(f"denied: refused to store {self.keywords}")

    def delete_password(self, service: str, name: str) -> None:
        raise RuntimeError(f"denied: refused to drop {self.keywords}")


#: One password, in every spelling a keyring backend might echo it back in. The ``@`` and
#: ``://`` variants matter on their own: a password may contain either, which makes the two kinds
#: of credential shape overlap, and whichever pass runs second truncates at the space.
SECRET_SHAPES = [
    "password='Zq7x PLUMBUS9'",
    "password=Zq7x\\ PLUMBUS9",
    'password="Zq7x PLUMBUS9"',
    "password=Zq7xPLUMBUS9",
    "password='Zq7x PLUMBUS9",
    "password='P@ssw0rd PLUMBUS9'",
    'password="P@ss PLUMBUS9"',
    "password=P@ss\\ PLUMBUS9",
    "password='P@ss PLUMBUS9",
    "password=P@ssPLUMBUS9",
    "sslpassword=p://x PLUMBUS9",
    "passfile=/x/p@ss PLUMBUS9",
    "sslkey=k://a PLUMBUS9",
]


def write_config(tmp_path, text: str = CONFIG):
    path = tmp_path / "invoicing.yaml"
    path.write_text(text)
    return path


def adopt(app: Application, path) -> Application:
    """Put a configuration into a window.

    The application only ever reads its own saved file, from a path nobody chooses — it has no Open
    button and takes no path on the command line, and `ConfigDocument` has no loader of its own. The
    harness uses the library's own loader, which the command-line tool uses and which is not part
    of the GUI, and puts the result in directly. One place touches the internals, deliberately,
    rather than keeping production code alive that only tests call.
    """
    ((_, config),) = load_config_files([path])
    app.config = ConfigDocument(path=path, config=config)
    app._adopt_documents()
    app._refresh()
    return app


def application(tmp_path, *, environ=None, keychain=None, text: str = CONFIG) -> Application:
    app = Application(
        environ=dict(BOTH) if environ is None else environ,
        keychain=FakeKeychain() if keychain is None else keychain,
    )
    return adopt(app, write_config(tmp_path, text))


@pytest.fixture
def app(tmp_path) -> Application:
    return application(tmp_path)


def set_output_directory(app: Application, value) -> None:
    """Point the reports at a directory, the way the window does.

    The field belongs to the configuration now, so writing the window property alone is reverted
    by the next refresh — exactly as a value typed into any other config field would be.
    """
    app.window.config_changed("output_dir", str(value))


def rows(model) -> list[dict[str, Any]]:
    return [dict(row) for row in model]


def by_label(app: Application) -> dict[str, dict[str, Any]]:
    return {row["label"]: row for row in rows(app.window.sources)}


def everything_rendered(app: Application) -> str:
    """Every string the window holds, for the one assertion worth repeating at every layer."""
    window = app.window
    return str(
        [
            rows(window.sources),
            rows(window.rules),
            rows(window.default_rules),
            rows(window.progress),
            rows(window.findings),
            window.status_message,
            window.keychain_problem,
            window.verdict,
            list(window.exclude_schemas),
            window.exclude_schemas_text,
        ]
    )


def ok_capture(source, *args, **kwargs) -> CaptureResult:
    return CaptureResult(
        label=source.label,
        inventory=inventory(table("public", "t", cols=[col("c")]), label=source.label),
    )


def drifted_capture(source, *args, **kwargs) -> CaptureResult:
    """The master has a column the target does not, so the comparison finds real drift."""
    columns = [col("c")] if source.label != "prod" else [col("c"), col("extra")]
    return CaptureResult(
        label=source.label,
        inventory=inventory(table("public", "t", cols=columns), label=source.label),
    )


def unreachable_target(source, *args, **kwargs) -> CaptureResult:
    if source.label == "prod":
        return ok_capture(source)
    return CaptureResult(label=source.label, error=f"{source.label}: cannot connect (host=qa)")


async def run_to_completion(app: Application) -> None:
    app.window.drive_start_run()
    await app.wait_for_idle()


class TestLoading:
    def test_opening_a_config_populates_the_window(self, app):
        assert [row["label"] for row in app.window.sources] == ["prod", "qa"]

    def test_the_window_shows_every_option(self, app):
        assert app.window.max_workers == 4
        assert app.window.fail_on == "error"
        assert app.window.parallel is True

    def test_a_source_says_whether_it_has_a_password_without_saying_what(self, tmp_path):
        """The one thing the window needs to know about a credential, and the only thing it gets.

        A DSN already in the environment counts: the command-line tool reads one and so does a
        run, so a source that can connect must not be shown as one that cannot.
        """
        app = application(tmp_path, environ={"PROD_DSN": PROD_DSN})
        assert by_label(app)["prod"]["has_password"] is True
        assert by_label(app)["qa"]["has_password"] is False
        assert SECRET not in everything_rendered(app)

    def test_a_source_with_nothing_filled_in_says_what_it_still_needs(self, tmp_path):
        app = application(tmp_path, environ={})
        missing = by_label(app)["qa"]["missing"]
        assert missing == "host, database, user, password"

    def test_what_is_still_needed_shrinks_as_the_parts_are_typed(self, app):
        for field, value in (("host", "db-qa"), ("database", "invoicing"), ("user", "app")):
            app.window.source_changed("qa", field, value)
        # The environment holds QA_DSN, so the password is already accounted for.
        assert by_label(app)["qa"]["missing"] == ""

    def test_no_credential_value_reaches_the_window(self, app):
        # The one assertion worth repeating at every layer.
        assert "postgresql://" not in str(rows(app.window.sources))

    def test_every_source_row_carries_every_field(self, app):
        for row in rows(app.window.sources):
            assert set(row) == SOURCE_ROW_FIELDS

    def test_the_bundled_defaults_are_shown_read_only_with_every_field(self, app):
        defaults = rows(app.window.default_rules)
        assert defaults, "the bundled ruleset should be visible"
        for row in defaults:
            assert set(row) == RULE_ROW_FIELDS
            assert row["read_only"] is True

    def test_rule_list_fields_are_strings_not_python_lists(self, app):
        kinds = [row["kinds"] for row in rows(app.window.default_rules)]
        assert kinds, "no default rules to check"
        assert all(isinstance(value, str) for value in kinds)
        assert not any(value.startswith("[") for value in kinds)

    def test_both_forms_of_exclude_schemas_are_bound(self, tmp_path):
        app = application(tmp_path, text=CONFIG + "exclude_schemas: [quartz, audit_archive]\n")
        assert list(app.window.exclude_schemas) == ["quartz", "audit_archive"]
        assert app.window.exclude_schemas_text == "quartz, audit_archive"

    def test_an_unreadable_ignores_file_still_leaves_a_usable_window(self, tmp_path):
        app = application(tmp_path, text=CONFIG + "ignores_file: ../nowhere/rules.yaml\n")
        # Declared but absent is allowed (the tab can create it); a broken one must not crash.
        assert app.ignores is not None


class TestEditing:
    def test_editing_a_field_updates_the_document_and_marks_it_dirty(self, app):
        app.window.source_changed("qa", "host", "db-qa-2")
        assert app.config.config.targets[0].host == "db-qa-2"
        assert app.window.dirty is True

    def test_adding_a_target_adds_a_row(self, app):
        app.window.add_target()
        assert len(list(app.window.sources)) == 3

    def test_a_duplicate_label_becomes_a_status_message_not_an_exception(self, app):
        app.window.add_target()
        app.window.source_changed(app.config.config.targets[-1].label, "label", "qa")
        assert app.window.status_is_error is True
        assert "qa" in app.window.status_message
        assert [s.label for s in app.config.config.sources].count("qa") == 1

    def test_validation_errors_become_a_status_message(self, app):
        app.window.source_changed("qa", "dsn_env", "")
        app.window.validate_config()
        assert app.window.status_is_error is True
        assert "dsn_env" in app.window.status_message

    def test_a_valid_config_says_so(self, app):
        app.window.validate_config()
        assert app.window.status_is_error is False
        assert "valid" in app.window.status_message

    def test_removing_a_target_removes_its_row(self, app):
        app.window.add_target()
        app.window.remove_target("target")
        assert [row["label"] for row in app.window.sources] == ["prod", "qa"]

    def test_the_master_cannot_be_removed(self, app):
        app.window.remove_target("prod")
        assert app.window.status_is_error is True
        assert [row["label"] for row in app.window.sources] == ["prod", "qa"]

    def test_a_config_level_option_is_editable_through_the_same_callback(self, app):
        app.window.config_changed("max_workers", "9")
        app.window.config_changed("parallel", "false")
        app.window.config_changed("fail_on", "warning")
        assert app.config.config.options.max_workers == 9
        assert app.config.config.options.parallel is False
        assert app.config.config.options.fail_on == "warning"
        assert app.window.max_workers == 9

    def test_an_out_of_range_option_is_reported_by_validate_rather_than_crashing(self, app):
        app.window.config_changed("max_workers", "999")
        app.window.validate_config()
        assert app.window.status_is_error is True
        assert "max_workers" in app.window.status_message

    def test_a_non_numeric_option_becomes_a_status_message(self, app):
        app.window.config_changed("connect_timeout_seconds", "soon")
        assert app.window.status_is_error is True
        assert app.config.config.options.connect_timeout_seconds == 10

    def test_exclude_schemas_round_trips_through_the_text_field(self, app):
        app.window.config_changed("exclude_schemas", "quartz, audit_archive")
        assert app.config.config.exclude_schemas == ("quartz", "audit_archive")
        assert list(app.window.exclude_schemas) == ["quartz", "audit_archive"]

    def test_a_half_typed_schema_list_is_not_rewritten_under_the_cursor(self, app):
        app.window.exclude_schemas_text = "quartz, "
        app.window.config_changed("exclude_schemas", "quartz, ")
        assert app.window.exclude_schemas_text == "quartz, "

    def test_schema_map_is_parsed_into_pairs(self, app):
        app.window.source_changed("qa", "schema_map", "acme-invoicing=inv_qa")
        assert app.config.config.targets[0].schema_map == {"acme-invoicing": "inv_qa"}
        assert rows(app.window.sources)[1]["schema_map"] == "acme-invoicing=inv_qa"

    def test_a_malformed_schema_map_becomes_a_status_message(self, app):
        app.window.source_changed("qa", "schema_map", "acme-invoicing")
        assert app.window.status_is_error is True
        assert app.config.config.targets[0].schema_map == {}

    def test_the_liquibase_location_is_two_fields_over_one_model(self, app):
        app.window.source_changed("qa", "liquibase_schema", "acme-invoicing")
        app.window.source_changed("qa", "liquibase_table", "DBCHANGELOG")
        liquibase = app.config.config.targets[0].liquibase
        assert (liquibase.schema_name, liquibase.table) == ("acme-invoicing", "DBCHANGELOG")
        row = rows(app.window.sources)[1]
        assert (row["liquibase_schema"], row["liquibase_table"]) == (
            "acme-invoicing",
            "DBCHANGELOG",
        )

    def test_a_liquibase_table_without_a_schema_is_refused(self, app):
        app.window.source_changed("qa", "liquibase_table", "DBCHANGELOG")
        assert app.window.status_is_error is True
        assert app.config.config.targets[0].liquibase is None

    def test_clearing_both_liquibase_fields_clears_the_reference(self, app):
        app.window.source_changed("qa", "liquibase_schema", "acme-invoicing")
        app.window.source_changed("qa", "liquibase_schema", "")
        assert app.config.config.targets[0].liquibase is None

    def test_a_pasted_connection_string_is_refused_before_it_can_be_saved(self, app):
        app.window.source_changed("qa", "host", QA_DSN)
        app.window.save_config()
        assert app.window.status_is_error is True
        assert SECRET not in app.window.status_message
        assert QA_DSN not in app.config.path.read_text()


class TestSaving:
    def test_saving_writes_the_file_and_clears_dirty(self, app, tmp_path):
        app.window.source_changed("qa", "host", "db-qa-2")
        app.window.save_config()
        assert app.window.dirty is False
        assert "db-qa-2" in (tmp_path / "invoicing.yaml").read_text()

    def test_a_failed_save_becomes_a_status_message(self, app, tmp_path):
        app.config.path = tmp_path / "absent" / "nested" / "out.yaml"
        app.window.save_config()
        assert app.window.status_is_error is True
        assert "Traceback" not in app.window.status_message

    def test_an_invalid_config_is_not_written(self, app, tmp_path):
        before = (tmp_path / "invoicing.yaml").read_text()
        app.window.source_changed("qa", "dsn_env", "")
        app.window.save_config()
        assert app.window.status_is_error is True
        assert (tmp_path / "invoicing.yaml").read_text() == before

    def test_saving_reports_the_path_and_nothing_else(self, tmp_path):
        """A previous version of this test asserted ``"comment" in status_message`` after a save,
        and passed on pytest's temp directory being named after the test — the message has only
        ever been ``Saved <path>.``. Whether comments survive is said when the file is *read*; see
        `TestReopeningTheSavedConfiguration`."""
        app = application(tmp_path, text="# the invoicing service\n" + CONFIG)
        app.window.save_config()
        assert app.window.status_is_error is False
        assert app.window.status_message == f"Saved {app.config.path}."


def fill(app: Application, label: str, **parts: str) -> None:
    """Type the connection parts for one source, as a person would."""
    for field, value in parts.items():
        app.window.source_changed(label, field, value)


class TestPasswords:
    """The window asks for a password and nothing else about where a credential lives.

    There is no Credentials tab any more and no environment variable to name: the parts are typed
    into the source's own card, the connection string is assembled here, and the password goes to
    the OS keychain and nowhere else.
    """

    def test_storing_a_password_marks_the_source_without_showing_it(self, app):
        fill(app, "qa", host="db-qa", database="invoicing", user="app")
        app.window.store_password("qa", SECRET)
        assert app.window.status_is_error is False
        assert by_label(app)["qa"]["has_password"] is True
        assert SECRET not in everything_rendered(app)

    def test_the_keychain_entry_is_a_connection_string_the_command_line_can_read(self, tmp_path):
        """What is stored is a DSN under the generated variable name, not a bare password.

        The command-line tool resolves a ``dsn_env`` and nothing else, so a password stored here
        has to arrive as the whole connection string or the configuration this window writes would
        only work inside this window.
        """
        keychain = FakeKeychain()
        app = application(tmp_path, environ={}, keychain=keychain)
        fill(app, "qa", host="db-qa", port="6432", database="invoicing", user="app")
        app.window.source_changed("qa", "sslmode", "verify-full")
        app.window.store_password("qa", "P@ss word/1")

        ((name, stored),) = keychain.entries.items()
        assert name == "QA_DSN", "the variable already named in the configuration is the one used"
        assert stored == (
            "postgresql://app:P%40ss%20word%2F1@db-qa:6432/invoicing?sslmode=verify-full"
        )

    def test_forgetting_a_password_leaves_the_source_needing_one(self, tmp_path):
        app = application(tmp_path, environ={}, keychain=FakeKeychain())
        fill(app, "qa", host="db-qa", database="invoicing", user="app")
        app.window.store_password("qa", SECRET)
        assert by_label(app)["qa"]["has_password"] is True
        app.window.forget_password("qa")
        assert by_label(app)["qa"]["has_password"] is False
        assert by_label(app)["qa"]["missing"] == "password"

    def test_forgetting_a_password_falls_back_to_the_environment(self, app):
        """A DSN in the environment still works, so forgetting the stored one is not fatal."""
        fill(app, "prod", host="db-prod", database="invoicing", user="app")
        app.window.store_password("prod", SECRET)
        app.window.forget_password("prod")
        assert by_label(app)["prod"]["has_password"] is True

    def test_the_keychain_wins_over_the_environment(self, tmp_path):
        keychain = FakeKeychain()
        app = application(tmp_path, keychain=keychain)
        fill(app, "prod", host="kc", database="db", user="u")
        app.window.store_password("prod", "stored")
        assert keychain.entries["PROD_DSN"] == "postgresql://u:stored@kc:5432/db"
        assert app.credentials.resolve("PROD_DSN").value == keychain.entries["PROD_DSN"]

    def test_an_empty_password_is_refused_with_a_message(self, tmp_path):
        app = application(tmp_path, environ={"PROD_DSN": PROD_DSN})
        app.window.store_password("qa", "   ")
        assert app.window.status_is_error is True
        assert by_label(app)["qa"]["has_password"] is False

    def test_renaming_a_source_takes_its_password_with_it(self, tmp_path):
        """The variable is generated from the label, so a rename moves the entry it is filed under.

        Without this the password is orphaned: the renamed source looks under a name nothing was
        ever stored against, and nobody can retype what they cannot read back.
        """
        keychain = FakeKeychain()
        app = application(tmp_path, environ={}, keychain=keychain)
        fill(app, "qa", host="db-qa", database="invoicing", user="app")
        app.window.store_password("qa", SECRET)
        stored = keychain.entries["QA_DSN"]

        app.window.source_changed("qa", "label", "staging")

        assert keychain.entries == {"DB_STAGING_DSN": stored}
        assert by_label(app)["staging"]["has_password"] is True

    def test_an_unavailable_keychain_is_explained_rather_than_hidden(self, tmp_path):
        application_ = adopt(Application(environ={}, keychain=None), write_config(tmp_path))
        assert application_.window.keychain_available is False
        assert application_.window.keychain_problem != ""

    def test_a_keychain_complaint_is_sanitised_on_its_way_to_the_property(self, tmp_path):
        app = application(tmp_path, keychain=BrokenKeychain())
        assert app.window.keychain_available is False
        problem = app.window.keychain_problem
        assert problem != ""
        assert SECRET not in problem
        assert "postgresql://" not in problem
        assert "\n" not in problem

    def test_typing_in_the_config_tab_does_not_re_read_the_keychain(self, tmp_path):
        """Every handler refreshes the window, and the keychain is not a free lookup."""

        class Counting(FakeKeychain):
            reads = 0

            def get_password(self, service, name):
                Counting.reads += 1
                return super().get_password(service, name)

        app = application(tmp_path, keychain=Counting())
        before = Counting.reads
        for text in ("d", "db", "db-", "db-q", "db-qa"):
            app.window.source_changed("qa", "host", text)
        assert Counting.reads == before

    def test_storing_a_password_is_seen_by_the_next_refresh(self, tmp_path):
        """The keychain is cached per variable, so both handlers have to invalidate it."""
        app = application(tmp_path, environ={"PROD_DSN": PROD_DSN})
        fill(app, "qa", host="db-qa", database="invoicing", user="app")
        assert by_label(app)["qa"]["has_password"] is False
        app.window.store_password("qa", SECRET)
        assert by_label(app)["qa"]["has_password"] is True
        app.window.forget_password("qa")
        assert by_label(app)["qa"]["has_password"] is False

    @pytest.mark.parametrize("shape", SECRET_SHAPES, ids=lambda s: s.split("=", 1)[1][:14])
    def test_a_quoted_or_escaped_password_is_redacted_whole(self, tmp_path, shape):
        """Not merely its first token: a space in the value must not end the redaction."""
        app = application(tmp_path, keychain=EchoingKeychain(shape))

        assert app.window.keychain_available is False
        app.window.store_password("qa", "Zq7x PLUMBUS9")
        app.window.forget_password("qa")

        for where, text in (
            ("keychain_problem", app.window.keychain_problem),
            ("status_message", app.window.status_message),
            ("everything", everything_rendered(app)),
        ):
            assert text != "" or where == "status_message"
            assert "PLUMBUS9" not in text, f"{where}: {text}"
            assert "Zq7x" not in text, f"{where}: {text}"

    def test_the_keyword_pass_runs_first(self):
        """The two kinds of shape overlap, so the order of the passes is the whole fix.

        Run the other way round, the token pass redacts ``password='P@ss`` and leaves ``word'``
        — the tail of the password — behind. This fails if the passes are swapped.
        """
        assert app_module._sanitise("password='P@ss word' host=db") == "*** host=db"
        assert app_module._sanitise("sslpassword=p://x word") == "***"

    def test_sanitising_a_long_message_does_not_hang_the_loop_thread(self):
        """`\\S*(?:://|@)\\S*` was quadratic: 200k characters took 103 seconds, on the thread
        that draws the window."""
        started = time.monotonic()
        for text in ("x" * 200_000, "a:b" * 60_000, "a@b" * 60_000, "password=" + "a" * 200_000):
            app_module._sanitise(text)
        assert time.monotonic() - started < 5, "the token scan has to stay linear"

    def test_sanitising_leaves_the_non_secret_context_alone(self):
        """Over-redaction would make an unreachable host unexplainable."""
        kept = app_module._sanitise("qa: cannot connect (host=db-qa, port=5432, user=app)")
        assert kept == "qa: cannot connect (host=db-qa, port=5432, user=app)"
        assert app_module._sanitise("refused postgresql://u:p@h/db") == "refused ***"
        assert app_module._sanitise("sslkey='a b' host=db") == "*** host=db"

    def test_a_credential_only_in_the_keychain_is_usable_without_the_environment(self, tmp_path):
        app = application(tmp_path, environ={}, keychain=FakeKeychain(QA_DSN=QA_DSN))
        assert by_label(app)["qa"]["has_password"] is True
        assert SECRET not in everything_rendered(app)


class TestIgnores:
    def test_adding_a_rule_invents_an_id_that_no_bundled_default_uses(self, app):
        app.window.add_rule()
        added = rows(app.window.rules)
        assert len(added) == 1
        defaults = {row["id"] for row in rows(app.window.default_rules)}
        assert added[0]["id"] not in defaults
        assert set(added[0]) == RULE_ROW_FIELDS

    def test_two_added_rules_do_not_collide(self, app):
        app.window.add_rule()
        app.window.add_rule()
        ids = [row["id"] for row in rows(app.window.rules)]
        assert len(set(ids)) == 2
        assert app.window.status_is_error is False

    def test_editing_a_rule_reports_a_pattern_that_can_never_match(self, app):
        app.window.add_rule()
        rule_id = rows(app.window.rules)[0]["id"]
        app.window.rule_changed(rule_id, "names", "quartz")
        assert app.window.status_is_error is True
        assert "names" in app.window.status_message

    def test_a_good_edit_clears_the_complaint(self, app):
        app.window.add_rule()
        rule_id = rows(app.window.rules)[0]["id"]
        app.window.rule_changed(rule_id, "names", "quartz")
        app.window.rule_changed(rule_id, "names", "quartz.*")
        assert app.window.status_is_error is False
        assert rows(app.window.rules)[0]["names"] == "quartz.*"

    def test_the_ruleset_option_shares_the_rule_changed_callback(self, app):
        app.window.rule_changed("", "case_insensitive_globs", "false")
        assert app.ignores.config.options.case_insensitive_globs is False
        assert app.window.case_insensitive_globs is False
        app.window.rule_changed("", "case_insensitive_globs", "true")
        assert app.ignores.config.options.case_insensitive_globs is True

    def test_removing_a_rule_removes_its_row(self, app):
        app.window.add_rule()
        app.window.remove_rule(rows(app.window.rules)[0]["id"])
        assert rows(app.window.rules) == []

    def test_saving_creates_a_ruleset_beside_the_config_and_names_it(self, app, tmp_path):
        app.window.add_rule()
        rule_id = rows(app.window.rules)[0]["id"]
        app.window.rule_changed(rule_id, "names", "quartz.*")
        app.window.rule_changed(rule_id, "statuses", "extra_in_target")
        app.window.save_ignores()
        written = tmp_path / "invoicing.ignores.yaml"
        assert app.window.status_is_error is False, app.window.status_message
        assert written.exists()
        assert "quartz.*" in written.read_text()
        assert app.config.config.ignores_file == "invoicing.ignores.yaml"
        assert app.window.dirty is True

    def test_an_invalid_ruleset_is_not_written(self, app, tmp_path):
        app.window.add_rule()
        app.window.save_ignores()
        assert app.window.status_is_error is True
        assert not (tmp_path / "invoicing.ignores.yaml").exists()


class TestTheSessionLifecycle:
    """How a Slint callback reaches the session, and what happens when it cannot."""

    def test_pressing_start_without_a_running_loop_explains_and_releases_the_gate(self, app):
        app.window.drive_start_run()
        assert app.window.running is False, "the gate must not stay latched"
        assert app.window.status_is_error is True
        assert app.window.run_start_enabled() is True

    @pytest.mark.asyncio
    async def test_the_session_is_called_on_the_loop_thread(self, app):
        seen: list[tuple[Any, Any]] = []
        real = Session.compare

        def spy(self, **kwargs):
            seen.append((asyncio.get_running_loop(), threading.current_thread()))
            return real(self, **kwargs)

        with mock.patch(CAPTURE, ok_capture), mock.patch.object(Session, "compare", spy):
            await run_to_completion(app)

        loop, thread = seen[0]
        assert loop is asyncio.get_running_loop()
        assert thread is threading.current_thread()

    @pytest.mark.asyncio
    async def test_only_one_session_is_ever_built_for_one_configuration(self, app):
        built: list[Session] = []

        class Counting(Session):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                built.append(self)

        with (
            mock.patch(CAPTURE, ok_capture),
            mock.patch.object(app_module, "Session", Counting),
        ):
            await run_to_completion(app)
            await run_to_completion(app)
            app.window.check_all()
            await app.wait_for_idle()

        assert len(built) == 1

    @pytest.mark.asyncio
    async def test_a_config_edit_is_honoured_by_the_next_run(self, app):
        """A session built once over a stale config would compare the wrong thing."""
        seen: list[tuple[str, ...]] = []

        def record(source, *args, **kwargs):
            seen.append(kwargs.get("exclude_schemas", ()))
            return ok_capture(source)

        with mock.patch(CAPTURE, record):
            await run_to_completion(app)
            app.window.config_changed("exclude_schemas", "quartz")
            await run_to_completion(app)

        assert seen[0] == ()
        assert seen[-1] == ("quartz",)

    @pytest.mark.asyncio
    async def test_a_second_start_while_one_runs_is_refused_without_disturbing_it(self, app):
        gate = threading.Event()
        entered = threading.Semaphore(0)

        def blocking(source, *args, **kwargs):
            entered.release()
            assert gate.wait(10)
            return ok_capture(source)

        with mock.patch(CAPTURE, blocking):
            app.window.drive_start_run()
            await asyncio.to_thread(entered.acquire, True, 10)
            # The markup disables Start, so reach past it the way a stale click would.
            app.window.start_run()
            assert app.window.status_is_error is True
            assert "already running" in app.window.status_message
            gate.set()
            await app.wait_for_idle()

        assert app.window.running is False
        assert app.window.verdict_level == "ok"


class TestRunning:
    @pytest.mark.asyncio
    async def test_a_clean_run_reports_ok_and_clears_running(self, app):
        with mock.patch(CAPTURE, ok_capture):
            await run_to_completion(app)
        window = app.window
        assert window.running is False
        assert window.verdict_level == "ok"
        assert window.results_banner_is_success() is True
        assert window.results_reports_enabled() is True

    @pytest.mark.asyncio
    async def test_progress_rows_are_complete_and_one_per_source(self, app):
        with mock.patch(CAPTURE, ok_capture):
            await run_to_completion(app)
        progress = rows(app.window.progress)
        assert [row["label"] for row in progress] == ["prod", "qa"]
        for row in progress:
            assert set(row) == PROGRESS_ROW_FIELDS
            assert row["state"] == "captured"
        assert app.window.run_progress_count() == 2

    @pytest.mark.asyncio
    async def test_drift_becomes_findings_with_every_field(self, app):
        with mock.patch(CAPTURE, drifted_capture):
            await run_to_completion(app)
        findings = rows(app.window.findings)
        assert findings, "the drifted fixture should produce findings"
        for row in findings:
            assert set(row) == FINDING_ROW_FIELDS
        assert app.window.verdict_level == "error"
        assert app.window.results_banner_is_success() is False
        assert app.window.total_findings == len(findings)

    @pytest.mark.asyncio
    async def test_a_partial_comparison_never_renders_as_success(self, app):
        with mock.patch(CAPTURE, unreachable_target):
            await run_to_completion(app)
        window = app.window
        assert window.verdict_level == "error"
        assert window.results_banner_is_success() is False
        assert "incomplete" in window.verdict

    @pytest.mark.asyncio
    async def test_allow_unreachable_never_turns_an_incomplete_run_green(self, app):
        app.window.allow_unreachable = True
        with mock.patch(CAPTURE, unreachable_target):
            await run_to_completion(app)
        assert app.window.verdict_level == "error"
        assert app.window.results_banner_is_success() is False
        assert "incomplete" in app.window.status_message

    @pytest.mark.asyncio
    async def test_a_cancelled_run_is_cancelled_even_with_every_source_captured(self, app):
        """The run-level outcome, never the progress rows."""
        gate = threading.Event()
        entered = threading.Semaphore(0)

        def blocking(source, *args, **kwargs):
            entered.release()
            assert gate.wait(10)
            return ok_capture(source)

        with mock.patch(CAPTURE, blocking):
            app.window.drive_start_run()
            await asyncio.to_thread(entered.acquire, True, 10)
            app.window.drive_cancel_run()
            gate.set()
            await app.wait_for_idle()

        window = app.window
        assert window.running is False
        assert window.verdict_level == "cancelled"
        assert window.results_banner_is_success() is False
        assert window.results_banner_kind() == "cancelled"
        assert window.results_reports_enabled() is False
        assert app.results is None
        assert rows(window.findings) == []
        # StatusLine is always on screen and has two colours. Green is the one that means "that
        # worked", so it must not contradict the banner.
        assert window.status_is_error is True, window.status_message
        assert window.status_message != ""

    @pytest.mark.asyncio
    async def test_a_cancelled_run_can_be_followed_by_a_successful_one(self, app):
        gate = threading.Event()
        entered = threading.Semaphore(0)

        def blocking(source, *args, **kwargs):
            entered.release()
            assert gate.wait(10)
            return ok_capture(source)

        with mock.patch(CAPTURE, blocking):
            app.window.drive_start_run()
            await asyncio.to_thread(entered.acquire, True, 10)
            app.window.drive_cancel_run()
            gate.set()
            await app.wait_for_idle()
        with mock.patch(CAPTURE, ok_capture):
            await run_to_completion(app)
        assert app.window.verdict_level == "ok"

    @pytest.mark.asyncio
    async def test_an_unexpected_failure_in_the_run_leaves_no_verdict_and_no_report(self, app):
        def explode(source, *args, **kwargs):
            raise RuntimeError("the driver fell over")

        with (
            mock.patch(BUILD_REPORT, side_effect=explode),
            mock.patch(CAPTURE, ok_capture),
        ):
            await run_to_completion(app)

        window = app.window
        assert window.running is False
        assert window.verdict_level == ""
        assert window.results_reports_enabled() is False
        assert window.status_is_error is True
        assert "Traceback" not in window.status_message
        assert "fell over" not in window.status_message

    @pytest.mark.asyncio
    async def test_a_library_failure_becomes_a_message_rather_than_a_verdict(self, app):
        def explode(*args, **kwargs):
            raise ProbeError("master 'prod' could not be inspected")

        with mock.patch(BUILD_REPORT, explode), mock.patch(CAPTURE, ok_capture):
            await run_to_completion(app)

        assert app.window.running is False
        assert app.window.verdict_level == ""
        assert app.window.status_is_error is True
        assert "could not be inspected" in app.window.status_message

    @pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
    @pytest.mark.asyncio
    async def test_running_is_cleared_even_when_the_handler_raises_unexpectedly(self, app):
        with (
            mock.patch.object(Application, "_rule_set", side_effect=RuntimeError("mis-wired")),
            contextlib.suppress(Exception),
        ):
            app.window.drive_start_run()
        assert app.window.running is False
        assert app.window.run_start_enabled() is True

    @pytest.mark.asyncio
    async def test_a_bad_baseline_path_is_refused_before_the_session_is_claimed(self, app):
        app.window.baseline_path = "/nowhere/at/all/report.json"
        with mock.patch(CAPTURE, ok_capture):
            app.window.drive_start_run()
            assert app.window.running is False
            assert app.window.status_is_error is True
            # The session was never claimed, so the next run works.
            app.window.baseline_path = ""
            await run_to_completion(app)
        assert app.window.verdict_level == "ok"

    @pytest.mark.asyncio
    async def test_a_baseline_sets_aside_the_findings_it_already_contains(self, app, tmp_path):
        with mock.patch(CAPTURE, drifted_capture):
            await run_to_completion(app)
        set_output_directory(app, tmp_path)
        app.window.write_reports()
        assert app.window.status_is_error is False, app.window.status_message

        before = app.window.verdict_level
        app.window.baseline_path = str(tmp_path / "report.json")
        with mock.patch(CAPTURE, drifted_capture):
            await run_to_completion(app)
        # Accepted findings are clamped, not deleted: they stay in the report tagged `baseline`.
        assert before == "error"
        assert app.window.verdict_level == "info"
        assert app.window.results_banner_is_success() is False
        assert all(row["suppressed_by"] == "baseline" for row in rows(app.window.findings))

    @pytest.mark.parametrize("gate", ["never", "any", "warning", "error"])
    def test_a_configured_gate_is_what_the_next_run_uses(self, tmp_path, gate):
        """The Run tab's combo box defaults to "error" and silently replaced the config's gate.

        Two visible widgets disagreed and the untouched one won, which also wrote the wrong
        fail_on into every report the Results tab writes.
        """
        app = application(tmp_path, text=CONFIG + f"options:\n  fail_on: {gate}\n")
        assert app.window.fail_on == gate
        assert app.window.run_fail_on == gate
        assert app._effective_config().options.fail_on == gate

    def test_an_explicit_run_tab_gate_still_wins_and_does_not_touch_the_file(self, tmp_path):
        app = application(tmp_path, text=CONFIG + "options:\n  fail_on: never\n")
        app.window.run_fail_on = "warning"
        app.window.source_changed("qa", "host", "db-qa-2")  # any edit refreshes the window
        assert app.window.run_fail_on == "warning", "a per-run choice must survive a refresh"
        assert app._effective_config().options.fail_on == "warning"
        assert app.config.config.options.fail_on == "never", "the file's own gate is untouched"

    def test_an_explicit_gate_survives_every_later_refresh(self, tmp_path):
        """It used to survive exactly one.

        `_seeded_gate` was re-read from the window after seeding, which adopted the operator's own
        choice as "ours", so the next refresh reverted it — towards the config's gate, which fails
        open. Every finished run refreshes, so `any`, `any`, then `never`.
        """
        app = application(tmp_path, text=CONFIG + "options:\n  fail_on: never\n")
        app.window.run_fail_on = "any"
        seen = []
        for index in range(5):
            app.window.source_changed("qa", "host", f"db-qa-{index}")  # any edit refreshes
            seen.append((app.window.run_fail_on, app._effective_config().options.fail_on))
        assert seen == [("any", "any")] * 5, seen
        assert app.config.config.options.fail_on == "never", "the file's own gate is untouched"

    @pytest.mark.asyncio
    async def test_an_explicit_gate_still_holds_after_two_completed_runs(self, tmp_path):
        app = application(tmp_path, text=CONFIG + "options:\n  fail_on: never\n")
        app.window.run_fail_on = "any"
        with mock.patch(CAPTURE, drifted_capture):
            await run_to_completion(app)
            assert app.window.run_fail_on == "any", "after one run"
            await run_to_completion(app)
            assert app.window.run_fail_on == "any", "after two runs"
            await run_to_completion(app)
        assert app.window.run_fail_on == "any", "the third run is still the operator's"
        assert app._effective_config().options.fail_on == "any"
        assert "--fail-on any" in app.window.verdict

    def test_editing_the_configs_gate_carries_the_run_tab_with_it(self, tmp_path):
        app = application(tmp_path)
        app.window.config_changed("fail_on", "any")
        assert (app.window.fail_on, app.window.run_fail_on) == ("any", "any")
        assert app._effective_config().options.fail_on == "any"

    @pytest.mark.asyncio
    async def test_a_configured_gate_reaches_the_written_report(self, app, tmp_path):
        app.window.config_changed("fail_on", "never")
        with mock.patch(CAPTURE, drifted_capture):
            await run_to_completion(app)
        assert "--fail-on never" in app.window.verdict
        set_output_directory(app, tmp_path)
        app.window.drive_write_reports()
        assert '"fail_on": "never"' in (tmp_path / "report.json").read_text()

    @pytest.mark.asyncio
    async def test_the_run_tabs_gate_reaches_the_report(self, app):
        app.window.run_fail_on = "never"
        with mock.patch(CAPTURE, drifted_capture):
            await run_to_completion(app)
        assert "--fail-on never" in app.window.verdict

    @pytest.mark.asyncio
    async def test_no_credential_reaches_any_property_during_a_run(self, app):
        with mock.patch(CAPTURE, unreachable_target):
            await run_to_completion(app)
        assert SECRET not in everything_rendered(app)
        assert "postgresql://" not in everything_rendered(app)


class TestChecking:
    @pytest.mark.asyncio
    async def test_checking_one_source_fills_in_its_row(self, app):
        status = ConnectionStatus(
            label="qa",
            ok=True,
            server_version="15.19 (Debian)",
            database="invoicing",
            user="app",
            schemas=("public",),
        )
        with mock.patch(CHECK, return_value=status):
            app.window.check_connection("qa")
            await app.wait_for_idle()
        row = rows(app.window.sources)[1]
        assert row["connection_ok"] is True
        assert row["checked"] is True
        assert "15.19" in row["connection_status"]
        assert set(row) == SOURCE_ROW_FIELDS

    @pytest.mark.asyncio
    async def test_an_unreachable_source_is_reported_without_its_credential(self, app):
        status = ConnectionStatus(label="qa", ok=False, error=f"qa: cannot connect using {QA_DSN}")
        with mock.patch(CHECK, return_value=status):
            app.window.check_connection("qa")
            await app.wait_for_idle()
        row = rows(app.window.sources)[1]
        assert row["connection_ok"] is False
        assert SECRET not in row["connection_status"]
        assert app.window.status_is_error is True

    @pytest.mark.asyncio
    async def test_check_all_fills_in_every_row(self, app):
        def check(source, *args, **kwargs):
            return ConnectionStatus(label=source.label, ok=True, server_version="15.19")

        with mock.patch(CHECK, check):
            app.window.check_all()
            await app.wait_for_idle()
        assert all(row["checked"] for row in rows(app.window.sources))
        assert app.window.status_is_error is False

    @pytest.mark.asyncio
    async def test_a_cancelled_check_is_not_reported_in_green(self, app):
        """Defensive: Cancel is gated on `running`, which only a comparison sets, so the UI
        cannot reach this today. Driven through the session the way a host could."""
        gate = threading.Event()
        entered = threading.Semaphore(0)

        def blocking(source, *args, **kwargs):
            entered.release()
            assert gate.wait(10)
            return ConnectionStatus(label=source.label, ok=True)

        with mock.patch(CHECK, blocking):
            app.window.check_all()
            await asyncio.to_thread(entered.acquire, True, 10)
            app._session.cancel()
            gate.set()
            await app.wait_for_idle()

        assert app.window.status_is_error is True, app.window.status_message
        assert "cancelled" in app.window.status_message.lower()
        assert not any(row["checked"] for row in rows(app.window.sources))

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("raises", "expected"),
        [(ProbeError("qa: server refused the connection"), "refused"), (RuntimeError("boom"), "")],
        ids=["library-error", "unexpected"],
    )
    async def test_a_check_that_fails_is_reported_in_red(self, app, raises, expected):
        """Defensive today — the session turns every per-source failure into a status value — but
        this is the "an error shown in green" class, so the polarity is pinned."""

        async def explode() -> list[ConnectionStatus]:
            raise raises

        with mock.patch.object(Session, "check_all", lambda self: explode()):
            app.window.check_all()
            await app.wait_for_idle()

        assert app.window.status_is_error is True, app.window.status_message
        assert expected in app.window.status_message
        assert "Traceback" not in app.window.status_message
        assert "boom" not in app.window.status_message

    def test_an_unknown_label_becomes_a_status_message(self, app):
        app.window.check_connection("nowhere")
        assert app.window.status_is_error is True
        assert "nowhere" in app.window.status_message

    @pytest.mark.asyncio
    async def test_a_check_does_not_disturb_the_last_run_s_verdict(self, app):
        with mock.patch(CAPTURE, ok_capture):
            await run_to_completion(app)
        with mock.patch(CHECK, return_value=ConnectionStatus(label="qa", ok=True)):
            app.window.check_connection("qa")
            await app.wait_for_idle()
        assert app.window.verdict_level == "ok"
        assert app.window.results_reports_enabled() is True


class TestStatusPolarity:
    """The toast has two colours, so every message picks one.

    A flip in either direction is invisible to a test that only checks the text, and the Task 10
    banner defect was exactly that. These assert the colour, not the words.
    """

    def test_every_routine_notice_is_green(self, app, tmp_path):
        app.choose_path = lambda purpose, current: str(tmp_path)

        def green(what: str) -> None:
            assert app.window.status_is_error is False, f"{what}: {app.window.status_message}"
            assert app.window.status_message != "", f"{what}: no message at all"

        # No "loading a config" case: the application does not open configuration files, and the
        # harness puts one in without going through a handler, so there is no notice to colour.
        app.window.add_target()
        green("adding a target")
        app.window.remove_target("target")
        green("removing a target")
        app.window.add_rule()
        green("adding a rule")
        app.window.remove_rule(rows(app.window.rules)[0]["id"])
        green("removing a rule")
        app.window.store_password("qa", SECRET)
        green("storing a password")
        app.window.forget_password("qa")
        green("forgetting a password")
        app.window.drive_choose_baseline()
        green("choosing a baseline")
        app.window.drive_choose_output_directory()
        green("choosing an output directory")
        # Not drive_cancel_run: the markup gates Cancel on `running`, so the handler's
        # nothing-to-do branch is only reachable the way a stale click would reach it.
        app.window.cancel_run()
        green("cancelling when nothing runs")
        assert app.window.running is False, "and the gate must not be left latched"

    @pytest.mark.asyncio
    async def test_a_finished_run_and_a_check_in_progress_are_green(self, app):
        with mock.patch(CHECK, return_value=ConnectionStatus(label="qa", ok=True)):
            app.window.check_connection("qa")
            assert app.window.status_is_error is False, "while the check is in flight"
            assert "Checking" in app.window.status_message
            await app.wait_for_idle()
        assert app.window.status_is_error is False, "a reachable source"

        with mock.patch(CAPTURE, ok_capture):
            await run_to_completion(app)
        assert app.window.status_is_error is False, app.window.status_message

    @pytest.mark.asyncio
    async def test_cancelling_explains_itself_in_red_while_it_drains(self, app):
        gate = threading.Event()
        entered = threading.Semaphore(0)

        def blocking(source, *args, **kwargs):
            entered.release()
            assert gate.wait(10)
            return ok_capture(source)

        with mock.patch(CAPTURE, blocking):
            app.window.drive_start_run()
            await asyncio.to_thread(entered.acquire, True, 10)
            app.window.drive_cancel_run()
            assert app.window.status_is_error is True, "the in-flight warning is not good news"
            assert "statement timeout" in app.window.status_message
            gate.set()
            await app.wait_for_idle()
        assert app.window.status_is_error is True

    def test_opening_the_written_report_is_green(self, app, tmp_path):
        async def go() -> None:
            with mock.patch(CAPTURE, drifted_capture):
                await run_to_completion(app)

        asyncio.run(go())
        app.open_url = lambda url: True
        set_output_directory(app, tmp_path)
        app.window.drive_write_reports()
        assert app.window.status_is_error is False
        app.window.drive_open_html()
        assert app.window.status_is_error is False, app.window.status_message
        assert "Opened" in app.window.status_message


class TestResults:
    @pytest.fixture
    def finished(self, app):
        async def go() -> None:
            with mock.patch(CAPTURE, drifted_capture):
                await run_to_completion(app)

        asyncio.run(go())
        return app

    def test_the_filter_reports_what_it_hides(self, finished):
        total = finished.window.total_findings
        assert total
        finished.window.drive_type_filter("nothing-matches-this")
        assert rows(finished.window.findings) == []
        assert finished.window.total_findings == total
        assert finished.window.results_filter_hides_rows() is True
        assert "hidden by the active filter" in finished.window.results_summary()

    def test_turning_off_every_severity_hides_every_row_and_says_so(self, finished):
        finished.window.drive_toggle_error(False)
        finished.window.drive_toggle_warning(False)
        finished.window.drive_toggle_info(False)
        assert rows(finished.window.findings) == []
        assert finished.window.results_filter_hides_rows() is True

    def test_writing_reports_writes_all_three(self, finished, tmp_path):
        set_output_directory(finished, tmp_path)
        finished.window.drive_write_reports()
        assert finished.window.status_is_error is False, finished.window.status_message
        for name in ("report.json", "junit.xml", "report.html"):
            assert (tmp_path / name).exists()

    def test_writing_without_a_directory_asks_for_one(self, finished):
        set_output_directory(finished, "")
        finished.window.drive_write_reports()
        assert finished.window.status_is_error is True

    def test_opening_the_html_report_needs_it_to_exist(self, finished, tmp_path):
        opened: list[str] = []
        finished.open_url = lambda url: bool(opened.append(url))
        set_output_directory(finished, tmp_path)
        finished.window.drive_open_html()
        assert finished.window.status_is_error is True
        assert opened == []

        finished.window.drive_write_reports()
        finished.window.drive_open_html()
        assert opened and opened[0].endswith("report.html")

    def test_nothing_can_be_written_before_a_run(self, app, tmp_path):
        set_output_directory(app, tmp_path)
        app.window.write_reports()
        assert app.window.status_is_error is True
        assert not list(tmp_path.glob("report.*"))

    def test_choosing_a_path_without_a_dialog_says_how_to_type_it(self, app):
        app.window.drive_choose_baseline()
        assert app.window.status_is_error is True
        assert "type" in app.window.status_message

    def test_a_host_supplied_chooser_is_used(self, app, tmp_path):
        app.choose_path = lambda purpose, current: str(tmp_path / f"{purpose}.json")
        app.window.drive_choose_baseline()
        assert app.window.baseline_path.endswith("baseline.json")
        app.window.drive_choose_output_directory()
        assert app.window.output_directory.endswith("output-directory.json")

    def test_cancelling_a_dialog_is_not_a_failure(self, app):
        """Dismissing a picker is an ordinary thing to do and must not look like an error.

        Both a cancelled dialog and a machine without one produce no path, so the two are told
        apart by `path_dialogs_available` — without it, every cancel would read as a fault.
        """
        app.path_dialogs_available = lambda: True
        app.choose_path = lambda purpose, current: None

        app.window.drive_choose_baseline()
        assert app.window.status_is_error is False
        assert app.window.baseline_path == ""

        app.window.drive_choose_output_directory()
        assert app.window.status_is_error is False
        assert app.window.output_directory == ""

    def test_cancelling_leaves_a_path_already_chosen_alone(self, app, tmp_path):
        app.path_dialogs_available = lambda: True
        app.choose_path = lambda purpose, current: str(tmp_path / "kept.json")
        app.window.drive_choose_baseline()
        app.choose_path = lambda purpose, current: None
        app.window.drive_choose_baseline()
        assert app.window.baseline_path.endswith("kept.json")

    def test_the_chooser_is_told_what_the_field_already_holds(self, app, tmp_path):
        """So the dialog opens where the person was last looking, not at some default."""
        asked: list[tuple[str, str]] = []
        app.window.baseline_path = str(tmp_path / "previous.json")
        app.choose_path = lambda purpose, current: asked.append((purpose, current)) or None
        app.path_dialogs_available = lambda: True
        app.window.drive_choose_baseline()
        assert asked == [("baseline", str(tmp_path / "previous.json"))]


class TestDetailPane:
    @staticmethod
    def show(app, model):
        app.results = model
        app._verdict = ("error", "x")
        app._refresh()
        return app

    @staticmethod
    def click(app, position=0):
        row = dict(app.window.findings[position])
        app.window.drive_select_row(position)
        return row

    def test_selecting_a_finding_fills_the_pane(self, app, model_with_a_changed_view):
        self.show(app, model_with_a_changed_view)
        row = self.click(app)
        assert app.window.status_is_error is False, app.window.status_message
        assert len(app.window.selected_deltas) == 1
        assert row["path"] in app.window.selected_heading
        assert row["target"] in app.window.selected_heading

    def test_every_delta_row_carries_every_field(self, app, model_with_a_changed_view):
        self.show(app, model_with_a_changed_view)
        self.click(app)
        assert set(dict(app.window.selected_deltas[0])) == {
            "attribute",
            "master",
            "target",
            "severity",
            "note",
            "is_body",
        }

    def test_a_body_finding_fills_the_diff(self, app, model_with_a_changed_view):
        self.show(app, model_with_a_changed_view)
        self.click(app)
        kinds = {dict(r)["kind"] for r in app.window.selected_diff}
        assert {"added", "removed"} <= kinds
        assert app.window.results_diff_count() == len(app.window.selected_diff)

    def test_the_click_resolves_the_identity_under_the_live_filter(
        self, app, model_with_two_findings
    ):
        self.show(app, model_with_two_findings)
        app.window.drive_type_filter("second")
        assert [r["path"] for r in rows(app.window.findings)] == ["public.second"]
        self.click(app, 0)
        assert "public.second" in app.window.selected_heading
        assert "y" in [dict(r)["text"] for r in app.window.selected_diff]

    def test_changing_the_filter_clears_the_pane(self, app, model_with_two_findings):
        self.show(app, model_with_two_findings)
        self.click(app, 1)
        assert app.window.selected_heading
        app.window.drive_type_filter("nomatch")
        assert app.window.selected_heading == ""
        assert len(app.window.selected_deltas) == 0
        assert len(app.window.selected_diff) == 0

    def test_toggling_a_severity_clears_the_pane(self, app, model_with_a_changed_view):
        self.show(app, model_with_a_changed_view)
        self.click(app)
        app.window.drive_toggle_info(False)
        assert app.window.selected_heading == ""
        assert len(app.window.selected_deltas) == 0

    def test_a_new_run_clears_the_pane(self, app, model_with_a_changed_view):
        self.show(app, model_with_a_changed_view)
        self.click(app)
        app.results = None
        app._refresh()
        assert app.window.selected_heading == ""
        assert len(app.window.selected_diff) == 0

    def test_selecting_before_a_run_is_harmless(self, app):
        app.select_finding("qa", "view", "public.v")
        assert len(app.window.selected_deltas) == 0
        assert app.window.selected_heading == ""

    def test_an_identity_the_live_filter_hides_leaves_an_empty_pane(
        self, app, model_with_two_findings
    ):
        self.show(app, model_with_two_findings)
        app.window.filter_text = "second"
        app.select_finding("qa", "view", "public.first")
        assert app.window.selected_heading == ""
        app.window.filter_text = ""
        app.window.show_error = False
        app.select_finding("qa", "view", "public.first")
        assert app.window.selected_heading == ""

    def test_an_unknown_identity_leaves_an_empty_pane(self, app, model_with_a_changed_view):
        self.show(app, model_with_a_changed_view)
        app.select_finding("nope", "view", "public.v_open")
        assert app.window.selected_heading == ""
        assert len(app.window.selected_deltas) == 0

    def test_clicking_a_missing_object_names_it_instead_of_clearing_the_pane(
        self, app, model_with_a_missing_and_an_extra_object
    ):
        # Three rows out of four used to read as a no-op: a finding with no deltas returned
        # through the same clear-the-pane path as one that does not exist. A missing object has
        # no attribute differences because there is no second version of it to differ from.
        self.show(app, model_with_a_missing_and_an_extra_object)
        for position in range(len(app.window.findings)):
            row = self.click(app, position)
            heading = app.window.selected_heading
            assert row["path"] in heading, heading
            assert row["target"] in heading, heading
            assert "no attribute differences" in heading, heading
            assert app.window.status_is_error is False, app.window.status_message
            assert len(app.window.selected_deltas) == 0
            assert len(app.window.selected_diff) == 0

    def test_the_heading_says_how_the_object_differs(
        self, app, model_with_a_missing_and_an_extra_object
    ):
        self.show(app, model_with_a_missing_and_an_extra_object)
        headings = []
        for position in range(len(app.window.findings)):
            self.click(app, position)
            headings.append(app.window.selected_heading)
        assert any("missing in target" in h for h in headings), headings
        assert any("extra in target" in h for h in headings), headings

    def test_a_differing_object_still_says_so_and_lists_its_deltas(
        self, app, model_with_a_changed_view
    ):
        self.show(app, model_with_a_changed_view)
        self.click(app)
        assert "differs" in app.window.selected_heading
        assert "no attribute differences" not in app.window.selected_heading
        assert len(app.window.selected_deltas) == 1

    def test_a_partial_row_becomes_a_status_error_not_a_crash(
        self, app, model_with_a_changed_view, monkeypatch
    ):
        self.show(app, model_with_a_changed_view)
        real = type(app.results).delta_rows

        def partial(self_, *args, **kwargs):
            return [
                {k: v for k, v in r.items() if k != "note"} for r in real(self_, *args, **kwargs)
            ]

        monkeypatch.setattr(type(app.results), "delta_rows", partial)
        self.click(app)
        assert app.window.status_is_error is True
        assert "note" in app.window.status_message
        assert len(app.window.selected_deltas) == 0

    def test_a_partial_diff_row_is_surfaced_too(self, app, model_with_a_changed_view, monkeypatch):
        self.show(app, model_with_a_changed_view)
        real = type(app.results).diff_rows

        def partial(self_, *args, **kwargs):
            return [
                {k: v for k, v in r.items() if k != "kind"} for r in real(self_, *args, **kwargs)
            ]

        monkeypatch.setattr(type(app.results), "diff_rows", partial)
        self.click(app)
        assert app.window.status_is_error is True
        assert "kind" in app.window.status_message

    def test_no_secret_reaches_the_detail_pane(self, app):
        from db_schema_diff.model.keys import column_key

        from .conftest import _model, delta, differing

        model = _model(
            differing(
                column_key("public", "t", "c"),
                delta("column.default", "'***:ab12cd34'", "'***:ee00ff11'"),
            )
        )
        self.show(app, model)
        self.click(app)
        blob = "".join(str(dict(r)) for r in app.window.selected_deltas)
        assert "s3cret" not in blob
        assert "***:" in blob


class TestEntryPoint:
    def test_run_returns_one_when_the_window_cannot_be_built(self):
        with mock.patch.object(app_module, "Application", side_effect=RuntimeError("no display")):
            assert app_module.run([]) == 1

    def test_a_path_on_the_command_line_opens_nothing_and_says_so(self, tmp_path, capsys):
        """A path on the command line is still refused, and said out loud.

        The application reads back the one configuration it saves, and nothing else. Silently
        ignoring a path would leave someone staring at a window wondering which file they were
        looking at — and they would be looking at the wrong one.
        """
        path = write_config(tmp_path)
        built: list[Application] = []
        original = Application.__init__

        def remember(self, *args, **kwargs):
            original(self, *args, **kwargs)
            built.append(self)

        with (
            mock.patch.object(app_module.slint, "run_event_loop", lambda coro: coro.close()),
            mock.patch.object(Application, "__init__", remember),
        ):
            assert app_module.run([str(path)]) == 0

        from db_schema_diff.gui import home

        assert built, "the window was never built"
        assert built[0].config.path != path, "the path on the command line was opened"
        assert built[0].config.path is None, "nothing was saved there, so nothing to reopen"
        said = capsys.readouterr().err
        assert "takes no arguments" in said
        assert str(home.default_path()) in said, "it has to say which file it does use"

    def test_run_installs_the_real_pickers(self, tmp_path):
        """The default chooser is inert, and `run` is what makes it real.

        This separation is load-bearing: with the real pickers as the Application's default, every
        test that built one opened a Finder window and blocked until someone dismissed it.
        """
        from db_schema_diff.gui import dialogs

        built: list[Application] = []
        original = Application.__init__

        def remember(self, *args, **kwargs):
            original(self, *args, **kwargs)
            built.append(self)

        with (
            mock.patch.object(app_module.slint, "run_event_loop", lambda coro: coro.close()),
            mock.patch.object(Application, "__init__", remember),
        ):
            assert app_module.run([]) == 0

        assert built, "run did not build an application"
        assert built[0].choose_path is dialogs.choose
        assert built[0].path_dialogs_available is dialogs.available

    def test_a_freshly_built_application_opens_no_dialog(self):
        """The guard on the above: a default that shows a dialog cannot be unit tested at all."""
        fresh = Application()
        assert fresh.choose_path("config", "/nowhere") is None
        assert fresh.path_dialogs_available() is False

    def test_the_collector_is_disabled_while_the_application_runs(self):
        """A Slint value freed by a worker thread's collection aborts the process.

        Demonstrated, not assumed: with the collector enabled, a thread allocating cyclic
        garbage while a window exists dies on
        ``PyStruct is unsendable, but sent to another thread``.
        """
        states: list[bool] = []
        # conftest turns the collector off for every test in this package, for the same reason.
        # This one is about run() restoring whatever it found, so it sets the state itself.
        enabled = gc.isenabled()
        gc.enable()
        try:
            with (
                mock.patch.object(app_module, "Application"),
                mock.patch.object(
                    app_module.slint,
                    "run_event_loop",
                    lambda coro: (states.append(gc.isenabled()), coro.close()),
                ),
            ):
                assert app_module.run([]) == 0

            assert states == [False], "the cyclic collector must not run on a capture thread"
            assert gc.isenabled() is True, "and the process it was handed back must be as it was"
        finally:
            if not enabled:
                gc.disable()

    @pytest.mark.asyncio
    async def test_the_loop_collects_the_garbage_itself(self):
        """Disabling the collector is only safe because the loop thread still runs it."""
        collected: list[int] = []
        window = mock.Mock()

        with mock.patch.object(app_module.gc, "collect", lambda: collected.append(1)):
            task = asyncio.ensure_future(app_module._show(window, interval=0.001))
            await asyncio.sleep(0.05)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        assert window.show.called
        assert collected, "the collector has to run somewhere, and the loop thread is the one"

    def test_the_default_store_reads_the_real_keychain(self):
        """No keychain argument means the real backend, asked for by CredentialStore itself."""
        real = app_module.CredentialStore
        seen: list[dict[str, Any]] = []

        def spy(*args: Any, **kwargs: Any):
            seen.append(kwargs)
            return real(
                backend=FakeKeychain(), environ=kwargs.get("environ"), _import_keyring=False
            )

        with mock.patch.object(app_module, "CredentialStore", spy):
            Application(environ={})
        assert seen == [{"environ": {}}]


class TestSshGateway:
    """Four widgets over one submodel, with the same shape as the liquibase pair.

    The rule being defended: the config file is committed, so it may hold a key *path* and a
    variable *name* — never a key and never a passphrase.
    """

    def test_naming_a_gateway_creates_the_block(self, app):
        app.window.source_changed("qa", "ssh_host", "bastion.internal")
        assert app.config.config.targets[0].ssh.host == "bastion.internal"
        assert app.config.dirty is True

    def test_the_other_fields_fill_in_around_it(self, app):
        app.window.source_changed("qa", "ssh_host", "bastion.internal")
        app.window.source_changed("qa", "ssh_user", "deploy")
        app.window.source_changed("qa", "ssh_key", "~/.ssh/id_ed25519")
        app.window.source_changed("qa", "ssh_passphrase_env", "QA_SSH_PASSPHRASE")

        ssh = app.config.config.targets[0].ssh
        assert (ssh.host, ssh.user) == ("bastion.internal", "deploy")
        assert (ssh.private_key, ssh.passphrase_env) == (
            "~/.ssh/id_ed25519",
            "QA_SSH_PASSPHRASE",
        )

    def test_a_key_with_no_gateway_is_refused(self, app):
        """A key with nothing to use it on cannot connect to anything.

        The liquibase pair refuses the same shape for the same reason: a half-filled submodel is a
        setting that cannot mean what it looks like it means.
        """
        app.window.source_changed("qa", "ssh_key", "~/.ssh/id_ed25519")
        assert app.window.status_is_error is True
        assert "gateway" in app.window.status_message
        assert app.config.config.targets[0].ssh is None

    def test_clearing_the_gateway_clears_the_whole_block(self, app):
        app.window.source_changed("qa", "ssh_host", "bastion.internal")
        app.window.source_changed("qa", "ssh_key", "~/.ssh/id_ed25519")
        app.window.source_changed("qa", "ssh_host", "")
        assert app.config.config.targets[0].ssh is None

    def test_a_source_with_no_gateway_sends_empty_strings_not_missing_keys(self, app):
        """Slint accepts a partial row and then dies at repaint, so every key must be present."""
        row = rows(app.window.sources)[0]
        assert row["ssh_host"] == ""
        assert set(row) == SOURCE_ROW_FIELDS

    def test_the_gateway_reaches_the_window(self, app):
        app.window.source_changed("qa", "ssh_host", "bastion.internal")
        app.window.source_changed("qa", "ssh_user", "deploy")
        row = next(r for r in rows(app.window.sources) if r["label"] == "qa")
        assert row["ssh_host"] == "bastion.internal"
        assert row["ssh_user"] == "deploy"

    def test_a_pasted_connection_string_is_refused_in_an_ssh_field(self, app, tmp_path):
        """Every free-text field on a source is a place someone might paste a DSN."""
        app.window.source_changed("qa", "ssh_host", "postgresql://u:s3cret@h/db")
        app.config.path = tmp_path / "c.yaml"
        with pytest.raises(GuiError):
            app.config.save()
        assert not (tmp_path / "c.yaml").exists()

    def test_no_passphrase_value_can_reach_the_config_file(self, app):
        """The field holds a variable name. Storing the value itself is what `dsn_env` prevents."""
        app.window.source_changed("qa", "ssh_host", "bastion.internal")
        app.window.source_changed("qa", "ssh_passphrase_env", "QA_SSH_PASSPHRASE")
        text = app.config.to_yaml()
        assert "QA_SSH_PASSPHRASE" in text
        assert "passphrase:" not in text


class TestTheGatewayStaysReadableInTheWindow:
    """`_sanitise` redacts any token holding `@` or `://` on its way to a property.

    That is why the tunnel names a gateway as "gateway host:port as user" and not the usual
    `user@host`. Spelled the usual way the whole token becomes `***`, and a person staring at a
    failed connection is told nothing at all. The two modules are checked together because each one
    alone looks correct.
    """

    def gateway(self, **kwargs):
        from db_schema_diff.config.model import SshRef
        from db_schema_diff.db.tunnel import describe

        return describe(SshRef(host="bastion.internal", port=2222, **kwargs))

    def test_a_gateway_failure_survives_sanitising_intact(self):
        message = f"{self.gateway(user='deploy')}: authentication was refused."
        cleaned = app_module._sanitise(message)
        assert "bastion.internal" in cleaned
        assert "2222" in cleaned
        assert "deploy" in cleaned
        assert "***" not in cleaned

    def test_the_usual_spelling_would_not_have_survived(self):
        """The counter-example, so the reason for the odd spelling is on the record."""
        assert app_module._sanitise("deploy@bastion.internal:2222 refused") == "*** refused"

    def test_a_passphrase_that_somehow_reached_a_message_would_still_be_cut(self):
        # The gateway description is safe by construction; this is the layer behind it.
        assert "hunter2" not in app_module._sanitise("sslpassword=hunter2 gateway b:22")


class TestTheKeyFilePicker:
    def test_choosing_a_key_fills_the_field_without_reading_the_file(self, app, tmp_path):
        """Only the path is taken. Nothing opens the key, here or anywhere in the GUI."""
        key = tmp_path / "id_ed25519"
        key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nsecret-material\n")
        app.window.source_changed("qa", "ssh_host", "bastion.internal")
        app.choose_path = lambda purpose, current: str(key)

        app.window.drive_choose_ssh_key("qa")

        assert app.config.config.targets[0].ssh.private_key == str(key)
        assert "secret-material" not in everything_rendered(app)

    def test_the_picker_opens_where_the_field_already_points(self, app, tmp_path):
        asked: list[tuple[str, str]] = []
        app.window.source_changed("qa", "ssh_host", "bastion.internal")
        app.window.source_changed("qa", "ssh_key", str(tmp_path / "old_key"))
        app.choose_path = lambda purpose, current: asked.append((purpose, current)) or None
        app.path_dialogs_available = lambda: True

        app.window.drive_choose_ssh_key("qa")

        assert asked == [("ssh-key", str(tmp_path / "old_key"))]

    def test_cancelling_leaves_the_field_alone(self, app, tmp_path):
        app.window.source_changed("qa", "ssh_host", "bastion.internal")
        app.window.source_changed("qa", "ssh_key", str(tmp_path / "kept"))
        app.choose_path = lambda purpose, current: None
        app.path_dialogs_available = lambda: True

        app.window.drive_choose_ssh_key("qa")

        assert app.config.config.targets[0].ssh.private_key == str(tmp_path / "kept")
        assert app.window.status_is_error is False

    def test_without_a_dialog_it_says_to_type_the_path(self, app):
        app.window.source_changed("qa", "ssh_host", "bastion.internal")
        app.window.drive_choose_ssh_key("qa")
        assert app.window.status_is_error is True
        assert "type" in app.window.status_message


class TestTheTunnelTestButton:
    def test_a_source_with_no_gateway_is_refused(self, app):
        """Nothing to test, and the message says which field is missing rather than failing late."""
        app.window.drive_test_tunnel("qa")
        assert app.window.status_is_error is True
        assert "no ssh gateway" in app.window.status_message

    @pytest.mark.asyncio
    async def test_a_reachable_gateway_is_reported_as_reached(self, app):

        app.window.source_changed("qa", "ssh_host", "bastion.internal")
        answer = TunnelStatus(label="qa", ok=True, detail="gateway bastion.internal:22: reached h")
        with mock.patch("db_schema_diff.runner.check_tunnel", return_value=answer):
            app.window.drive_test_tunnel("qa")
            await app.wait_for_idle()

        assert app.window.tunnel_dialog_open() is True
        assert app.window.tunnel_ok is True
        assert app.window.tunnel_busy is False
        assert "bastion.internal" in app.window.tunnel_summary

    @pytest.mark.asyncio
    async def test_a_refused_gateway_is_an_error_naming_the_gateway(self, app):

        app.window.source_changed("qa", "ssh_host", "bastion.internal")
        answer = TunnelStatus(
            label="qa",
            ok=False,
            detail="gateway bastion.internal:22: authentication was refused.",
        )
        with mock.patch("db_schema_diff.runner.check_tunnel", return_value=answer):
            app.window.drive_test_tunnel("qa")
            await app.wait_for_idle()

        assert app.window.tunnel_ok is False
        # The gateway stays legible: this is what the odd "gateway host:port" spelling buys.
        assert "bastion.internal" in app.window.tunnel_summary
        assert "***" not in app.window.tunnel_summary

    @pytest.mark.asyncio
    async def test_a_passphrase_cannot_reach_the_window_through_the_result(self, app):

        app.window.source_changed("qa", "ssh_host", "bastion.internal")
        answer = TunnelStatus(label="qa", ok=False, detail="refused sslpassword=hunter2")
        with mock.patch("db_schema_diff.runner.check_tunnel", return_value=answer):
            app.window.drive_test_tunnel("qa")
            await app.wait_for_idle()

        assert "hunter2" not in everything_rendered(app)
        assert "hunter2" not in app.window.tunnel_summary


class TestTheCardTabSurvivesARefresh:
    """Reported from use: cancelling the key picker jumped the card back to the Database tab.

    The cause was not the dialog. Every refresh replaces the whole row model, which rebuilds each
    repeated card and resets a property the card owned — so the tab reset on *any* refresh: a field
    edit, a connection check, anything. Cancelling was simply where nothing else changed to hide it.
    The tab now belongs to the view model, keyed by label like every other per-source thing here.
    """

    def tab_of(self, app, label):
        return next(r for r in rows(app.window.sources) if r["label"] == label)["tab"]

    def test_the_tab_starts_on_the_database(self, app):
        assert self.tab_of(app, "qa") == 0

    def test_cancelling_the_key_picker_leaves_the_tab_alone(self, app):
        app.window.drive_select_source_tab("qa", 1)
        app.window.source_changed("qa", "ssh_host", "bastion.internal")
        app.choose_path = lambda purpose, current: None
        app.path_dialogs_available = lambda: True

        app.window.drive_choose_ssh_key("qa")

        assert self.tab_of(app, "qa") == 1

    def test_choosing_a_key_also_leaves_the_tab_alone(self, app, tmp_path):
        app.window.drive_select_source_tab("qa", 1)
        app.window.source_changed("qa", "ssh_host", "bastion.internal")
        app.choose_path = lambda purpose, current: str(tmp_path / "id_ed25519")

        app.window.drive_choose_ssh_key("qa")

        assert self.tab_of(app, "qa") == 1

    def test_editing_any_field_leaves_the_tab_alone(self, app):
        """The general case. The dialog was never special; the refresh was."""
        app.window.drive_select_source_tab("qa", 1)
        app.window.source_changed("qa", "host", "db-qa.internal")
        assert self.tab_of(app, "qa") == 1

    def test_each_card_keeps_its_own_tab(self, app):
        app.window.drive_select_source_tab("qa", 1)
        assert self.tab_of(app, "qa") == 1
        assert self.tab_of(app, "prod") == 0

    def test_a_renamed_source_keeps_the_tab_it_was_on(self, app):
        app.window.drive_select_source_tab("qa", 1)
        app.window.source_changed("qa", "label", "qa2")
        assert self.tab_of(app, "qa2") == 1


class TestRetypingALabel:
    """Reported: the master's label could not be saved.

    Clearing a label and typing a new one is the ordinary way to rename something, and
    ``source-changed`` used an empty label to mean "a config-level field, not a source". So the
    moment the field was backspaced to nothing, the source it belonged to could no longer be
    addressed: every further keystroke was reported as an unknown configuration option, the label
    stayed empty, and Save then refused the configuration — with no way to fix it in the window.

    It bit the master first because a target can be removed and added again, and the master cannot.
    """

    def labels(self, app):
        return [row["label"] for row in rows(app.window.sources)]

    def keystrokes(self, app, texts):
        """Type into the first card's label field, as ``LineEdit.edited`` reports it."""
        for text in texts:
            app.window.source_changed(self.labels(app)[0], "label", text)

    def test_the_master_label_can_be_cleared_and_typed_again(self, app):
        self.keystrokes(app, ["pro", "pr", "p", "", "m", "ma", "mast", "master"])
        assert self.labels(app) == ["master", "qa"]
        assert app.window.status_is_error is False

    def test_a_target_label_can_be_cleared_and_typed_again(self, app):
        for text in ["q", "", "s", "st", "stage"]:
            app.window.source_changed(self.labels(app)[1], "label", text)
        assert self.labels(app) == ["prod", "stage"]
        assert app.window.status_is_error is False

    def test_an_emptied_label_is_still_the_source_it_belongs_to(self, app):
        """The one assertion the bug would fail: an empty label addresses a source, not an option.

        A config-level field has its own callback now, so there is no spelling of a source edit
        that means something else.
        """
        self.keystrokes(app, [""])
        assert self.labels(app) == ["", "qa"]
        app.window.source_changed("", "host", "db-prod")
        assert app.window.status_is_error is False
        assert rows(app.window.sources)[0]["host"] == "db-prod"

    def test_a_cleared_label_is_refused_on_save_rather_than_silently_written(self, app):
        self.keystrokes(app, [""])
        app.window.save_config()
        assert app.window.status_is_error is True
        assert "label" in app.window.status_message

    def test_the_second_source_cannot_also_be_emptied(self, app):
        """Two sources addressed by one empty label is a state neither could be typed out of."""
        self.keystrokes(app, [""])
        app.window.source_changed("qa", "label", "")
        assert app.window.status_is_error is True
        assert "empty" in app.window.status_message
        assert self.labels(app) == ["", "qa"]

    def test_a_renamed_master_still_carries_the_accent(self, app):
        """``is_master`` is derived from the label, so a rename must not move it to the target."""
        self.keystrokes(app, ["", "m", "main"])
        assert [row["is_master"] for row in rows(app.window.sources)] == [True, False]


class TestTypingDoesNotDestroyTheFieldBeingTypedInto:
    """Reported from use: every text field lost focus after one character.

    Every edit ends in a refresh, and the refresh replaced the whole row model. Replacing a model
    rebuilds every repeated element under it — including the LineEdit the person was typing into,
    which is destroyed and recreated empty of focus. Rows are now written into the existing model,
    and only a change in how many there are rebuilds it.

    Focus itself cannot be observed from here, so what these pin is the thing that destroys it:
    whether the model survived the edit.
    """

    def model_id(self, app, name):
        return id(getattr(app.window, name))

    def test_editing_a_source_keeps_the_same_model(self, app):
        before = self.model_id(app, "sources")
        app.window.source_changed("qa", "host", "d")
        app.window.source_changed("qa", "host", "db")
        assert self.model_id(app, "sources") == before

    def test_editing_a_source_still_updates_what_it_shows(self, app):
        """In place, but not inert: the row must still carry the new value."""
        app.window.source_changed("qa", "host", "db-qa.internal")
        row = next(r for r in rows(app.window.sources) if r["label"] == "qa")
        assert row["host"] == "db-qa.internal"

    def test_adding_a_target_does_rebuild_it(self, app):
        """A different number of rows has to rebuild, and nothing is being typed into then."""
        before = self.model_id(app, "sources")
        app.window.add_target()
        assert self.model_id(app, "sources") != before
        assert len(rows(app.window.sources)) == 3

    def test_removing_a_target_rebuilds_it_too(self, app):
        app.window.add_target()
        before = len(rows(app.window.sources))
        app.window.remove_target(rows(app.window.sources)[-1]["label"])
        assert len(rows(app.window.sources)) == before - 1

    def test_a_renamed_source_keeps_the_model_so_the_label_field_keeps_focus(self, app):
        """The label field is the one that would otherwise rebuild on its own keystrokes."""
        before = self.model_id(app, "sources")
        app.window.source_changed("qa", "label", "q")
        assert self.model_id(app, "sources") == before

    def test_editing_an_ignore_rule_keeps_its_model(self, app):
        """The rules list has editable fields of its own, and the same refresh behind them.

        A rule has to exist first: with none, the model is never assigned and every read returns a
        fresh empty default, which would make this pass without proving anything.
        """
        app.window.add_rule()
        rule_id = rows(app.window.rules)[0]["id"]
        before = self.model_id(app, "rules")
        app.window.rule_changed(rule_id, "reason", "quartz is runtime state")
        assert self.model_id(app, "rules") == before
        assert rows(app.window.rules)[0]["reason"] == "quartz is runtime state"

    def test_the_password_field_survives_a_refresh_of_its_own_row(self, app):
        """The password field holds a typed secret and is not bound to the model at all.

        Rebuilding the row would clear it mid-entry, with no indication why — and the password is
        the one field in the window that cannot be read back from anywhere to retype it.
        """
        before = self.model_id(app, "sources")
        app.window.source_changed("qa", "host", "x")
        assert self.model_id(app, "sources") == before


class TestTestingAGatewayDoesNotNeedTheDatabaseCredential:
    """Reported from use: filling in the gateway and pressing Test tunnel raised
    MissingCredentialsError, with everything filled in except the optional passphrase.

    The error was about the DSN, not the passphrase. The check resolved it only to learn the
    database's address — but somebody filling in gateway fields has usually not set the database
    credential yet, so checking an ssh key depended on a secret that has nothing to do with the key.
    That made the two checks one again, which is the thing this button exists to avoid.
    """

    @pytest.mark.asyncio
    async def test_the_gateway_is_still_tested_without_a_dsn(self, app, monkeypatch):
        from db_schema_diff.errors import MissingCredentialsError

        app.window.source_changed("qa", "ssh_host", "bastion.internal")
        monkeypatch.setattr(
            app.credentials,
            "resolve",
            mock.Mock(side_effect=MissingCredentialsError("$QA_DSN is not set")),
        )
        seen: dict[str, object] = {}

        def record(source, dsn, *, ssh_passphrase=None, observer=None):
            seen["dsn"] = dsn

            return TunnelStatus(label="qa", ok=True, detail="gateway bastion.internal:22: reached")

        with mock.patch("db_schema_diff.runner.check_tunnel", side_effect=record):
            app.window.drive_test_tunnel("qa")
            await app.wait_for_idle()

        assert seen["dsn"] is None, "the check should run without a database credential"
        assert app.window.tunnel_ok is True

    @pytest.mark.asyncio
    async def test_a_named_but_unset_passphrase_says_so_in_its_own_words(self, app, monkeypatch):
        """The library's wording tells people to set it to a libpq connection string, which a key
        passphrase is not. That sends them to the wrong place."""
        from db_schema_diff.errors import MissingCredentialsError

        app.window.source_changed("qa", "ssh_host", "bastion.internal")
        app.window.source_changed("qa", "ssh_passphrase_env", "QA_SSH_PASSPHRASE")
        monkeypatch.setattr(
            app.credentials,
            "resolve_secret",
            mock.Mock(side_effect=MissingCredentialsError("missing")),
        )

        app.window.drive_test_tunnel("qa")
        await app.wait_for_idle()

        message = app.window.tunnel_summary
        assert app.window.tunnel_ok is False
        assert "QA_SSH_PASSPHRASE" in message
        assert "libpq" not in message
        assert "clear the field" in message


class TestTheRunDialog:
    """What is being compared, and how far it has got.

    The report says what differed; without this nothing said what was *looked at*, so a clean
    report could not be told from a report of the wrong thing — wrong schemas, wrong gate, a rule
    suppressing the finding you were hunting.
    """

    def steps(self, app) -> list[dict[str, Any]]:
        return rows(app.window.run_steps)

    def labelled(self, app) -> dict[str, dict[str, Any]]:
        return {str(row["label"]): row for row in self.steps(app)}

    def inputs(self, app) -> list[str]:
        return [f"{row['label']}|{row['value']}" for row in rows(app.window.run_inputs)]

    @pytest.mark.asyncio
    async def test_it_opens_before_anything_has_been_compared(self, app):
        """A capture can take minutes. A dialog that appears only at the end explains nothing."""
        with mock.patch(CAPTURE, ok_capture):
            app.window.drive_start_run()
            # Asserted before the loop turns: the dialog is laid out by the click, not by a result.
            assert app.window.run_dialog_open() is True
            assert app.window.run_busy is True
            assert app.window.run_summary == ""
            assert {str(row["state"]) for row in self.steps(app)} == {"pending"}
            await app.wait_for_idle()

    @pytest.mark.asyncio
    async def test_the_whole_checklist_is_laid_out_up_front(self, app):
        """Not grown as results arrive: a list that grows cannot be told from one that stalled."""
        with mock.patch(CAPTURE, ok_capture):
            app.window.drive_start_run()
            labels = [str(row["label"]) for row in self.steps(app)]
            await app.wait_for_idle()
        assert "prod (master)" in labels
        assert "qa (target)" in labels
        assert labels.count("tables") == 2, "one per source"
        assert labels[-2:] == ["compare", "build report"]

    @pytest.mark.asyncio
    async def test_it_says_what_is_being_compared(self, app):
        app.window.source_changed("prod", "host", "db-prod.internal")
        app.window.source_changed("prod", "database", "invoicing")
        app.window.source_changed("qa", "schemas", "acme-invoicing, public")
        with mock.patch(CAPTURE, ok_capture):
            app.window.drive_start_run()
            await app.wait_for_idle()

        shown = " ".join(self.inputs(app))
        assert "db-prod.internal/invoicing" in shown
        assert "acme-invoicing, public" in shown
        assert "fail on error" in shown

    @pytest.mark.asyncio
    async def test_a_source_with_no_host_is_described_by_its_variable(self, app):
        """A hand-written configuration names only a variable, and the dialog must not be blank."""
        with mock.patch(CAPTURE, ok_capture):
            app.window.drive_start_run()
            await app.wait_for_idle()
        assert any("$QA_DSN" in line for line in self.inputs(app))

    def test_no_credential_reaches_the_dialog(self, app):
        """Every line comes from the configuration, which is non-secret by construction. Nothing
        here assembles a connection string to describe a source."""
        app.window.source_changed("prod", "host", "db-prod.internal")
        app.window.source_changed("prod", "user", "app")
        app.window.drive_start_run()
        shown = " ".join(self.inputs(app)) + " ".join(str(r) for r in self.steps(app))
        assert SECRET not in shown
        assert "postgresql://" not in shown

    @pytest.mark.asyncio
    async def test_the_steps_fill_in_as_the_capture_reports(self, app):
        def reporting(source, dsn, *, observer=None, **kwargs):
            observer("tables", 12)
            observer("columns", 97)
            return ok_capture(source)

        with mock.patch(CAPTURE, reporting):
            await run_to_completion(app)

        shown = self.labelled(app)
        assert (shown["tables"]["state"], shown["tables"]["detail"]) == ("ok", "12")
        assert (shown["columns"]["state"], shown["columns"]["detail"]) == ("ok", "97")

    @pytest.mark.asyncio
    async def test_the_schemas_actually_matched_are_named(self, app):
        """The one line that explains a wall of zeros.

        Reported: "when comparing I see mostly 0 objects". The cause was a ``schemas:`` filter
        matching no schema on the server, and the inputs block only said what had been *asked
        for*. Every count read zero with no reason given for any of them.
        """

        def reporting(source, dsn, *, observer=None, **kwargs):
            observer("schemas", 2, "acme-invoicing, public")
            observer("tables", 4, "")
            return ok_capture(source)

        with mock.patch(CAPTURE, reporting):
            await run_to_completion(app)

        schemas = self.labelled(app)["schemas"]
        assert str(schemas["detail"]) == "acme-invoicing, public"
        assert str(schemas["state"]) == "ok"

    @pytest.mark.asyncio
    async def test_matching_no_schema_is_marked_failed_on_the_schema_row(self, app):
        """Not "ok, zero": the filter did not do what it was asked, and that is the fault."""

        def reporting(source, dsn, *, observer=None, **kwargs):
            observer("schemas", 0, "none matched")
            return ok_capture(source)

        with mock.patch(CAPTURE, reporting):
            await run_to_completion(app)

        assert str(self.labelled(app)["schemas"]["state"]) == "failed"
        assert "none matched" in str(self.labelled(app)["schemas"]["detail"])

    @pytest.mark.asyncio
    async def test_materialized_views_get_a_count_of_their_own(self, app):
        """One query returns both, so a single "views: 3" could not answer how many matviews a
        database has — and a matview holds data and must be refreshed, so it is a different
        thing."""

        def reporting(source, dsn, *, observer=None, **kwargs):
            observer("views", 3, "")
            observer("plain views", 2, "")
            observer("materialized views", 1, "")
            return ok_capture(source)

        with mock.patch(CAPTURE, reporting):
            await run_to_completion(app)

        shown = self.labelled(app)
        assert str(shown["views"]["detail"]) == "3"
        assert str(shown["plain views"]["detail"]) == "2"
        assert str(shown["materialized views"]["detail"]) == "1"

    @pytest.mark.asyncio
    async def test_a_sub_row_sits_deeper_than_the_step_it_breaks_down(self, app):
        with mock.patch(CAPTURE, ok_capture):
            app.window.drive_start_run()
            await app.wait_for_idle()
        shown = self.labelled(app)
        assert int(shown["views"]["indent"]) == 1
        assert int(shown["materialized views"]["indent"]) == 2
        assert int(shown["prod (master)"]["indent"]) == 0

    @pytest.mark.asyncio
    async def test_a_capture_that_found_nothing_is_flagged_on_its_heading(self, app):
        """A source that connected and read nothing. Said per source, because a schema filter can
        match on one server and not on another."""

        def nothing(source, dsn, *, observer=None, **kwargs):
            observer("schemas", 0, "none matched")
            for step in ("tables", "columns"):
                observer(step, 0, "")
            return ok_capture(source)

        with mock.patch(CAPTURE, nothing):
            await run_to_completion(app)

        heading = self.labelled(app)["prod (master)"]
        assert str(heading["state"]) == "failed"
        assert "no objects" in str(heading["detail"])

    @pytest.mark.asyncio
    async def test_the_changelog_does_not_count_as_an_object(self, app):
        """A database with a DATABASECHANGELOG and no schema of its own has been looked at and
        found empty; counting its changesets would hide that."""

        def only_changelog(source, dsn, *, observer=None, **kwargs):
            observer("schemas", 0, "none matched")
            observer("changelog", 3, "")
            return ok_capture(source)

        with mock.patch(CAPTURE, only_changelog):
            await run_to_completion(app)

        assert str(self.labelled(app)["prod (master)"]["state"]) == "failed"

    @pytest.mark.asyncio
    async def test_a_capture_that_found_objects_says_how_many(self, app):
        def reporting(source, dsn, *, observer=None, **kwargs):
            observer("tables", 4, "")
            observer("columns", 20, "")
            return ok_capture(source)

        with mock.patch(CAPTURE, reporting):
            await run_to_completion(app)

        assert str(self.labelled(app)["prod (master)"]["detail"]) == "24 objects"

    @pytest.mark.asyncio
    async def test_a_sub_row_is_not_added_to_the_total_twice(self, app):
        """A sub-row is a breakdown of its step, so counting both would double every view."""

        def reporting(source, dsn, *, observer=None, **kwargs):
            observer("views", 3, "")
            observer("plain views", 2, "")
            observer("materialized views", 1, "")
            return ok_capture(source)

        with mock.patch(CAPTURE, reporting):
            await run_to_completion(app)

        assert str(self.labelled(app)["prod (master)"]["detail"]) == "3 objects"

    @pytest.mark.asyncio
    async def test_a_step_that_never_reported_is_skipped_not_left_pending(self, app):
        """A source that cannot connect reports nothing. Ten rows still saying "pending" would
        read as a run that stalled there rather than one that never got in."""
        with mock.patch(CAPTURE, unreachable_target):
            await run_to_completion(app)

        states = {str(row["state"]) for row in self.steps(app)}
        assert "pending" not in states

    @pytest.mark.asyncio
    async def test_the_phases_after_the_captures_are_shown(self, app):
        """Both are invisible otherwise: the run looks finished at the last capture, then sits."""
        with mock.patch(CAPTURE, ok_capture):
            await run_to_completion(app)

        shown = self.labelled(app)
        assert str(shown["compare"]["state"]) == "ok"
        assert str(shown["build report"]["state"]) == "ok"

    @pytest.mark.asyncio
    async def test_it_stays_open_with_the_verdict_when_the_run_finishes(self, app):
        with mock.patch(CAPTURE, ok_capture):
            await run_to_completion(app)

        assert app.window.run_dialog_open() is True, "the log is the transparency; it stays"
        assert app.window.run_busy is False
        assert app.window.run_summary == app.window.verdict
        assert app.window.run_summary_level == "ok"

    @pytest.mark.asyncio
    async def test_a_cancelled_run_releases_the_dialog(self, app):
        """A dialog left busy shows Cancel and no way out — a scrim with nothing to click."""
        with mock.patch(CAPTURE, ok_capture):
            app.window.drive_start_run()
            app.window.cancel_run()
            await app.wait_for_idle()

        assert app.window.run_busy is False
        assert app.window.run_summary != ""

    @pytest.mark.asyncio
    async def test_a_failed_run_says_why_in_the_dialog(self, app):
        """There is no verdict without a report, so the dialog must not imply one."""
        with mock.patch(CAPTURE, mock.Mock(side_effect=RuntimeError("boom"))):
            await run_to_completion(app)

        assert app.window.run_busy is False
        assert app.window.run_summary_level == "error"
        assert app.window.run_summary != ""

    @pytest.mark.asyncio
    async def test_the_model_is_updated_in_place_rather_than_replaced(self, app):
        """Replacing it rebuilds every repeated element under it, which throws away the scroll
        position halfway through a run — the same mechanism that cost a text field its focus."""
        app.window.drive_start_run()
        before = id(app.window.run_steps)

        def reporting(source, dsn, *, observer=None, **kwargs):
            observer("tables", 1)
            return ok_capture(source)

        with mock.patch(CAPTURE, reporting):
            await app.wait_for_idle()

        assert id(app.window.run_steps) == before

    def test_cancel_from_the_dialog_reaches_the_session(self, app):
        """The only way out of a long run, and it is behind the scrim."""
        with mock.patch.object(app, "_cancel_run", wraps=app._cancel_run) as spy:
            app.window.cancel_run = app._guard(spy)
            app.window.drive_cancel_from_run_dialog()
        assert spy.called

    def test_close_dismisses_it_without_moving_you(self, app):
        app.window.drive_start_run()
        app.window.current_view = 1
        app.window.drive_close_run_dialog()
        assert app.window.run_dialog_open() is False
        assert app.window.current_view == 1

    def test_show_results_closes_it_and_goes_to_the_results(self, app):
        app.window.drive_start_run()
        app.window.drive_show_run_results()
        assert app.window.run_dialog_open() is False
        assert app.window.current_view == 2
        assert app.window.view_visible(2) is True


class TestTheOutputDirectoryIsRemembered:
    """Where reports go is a property of the comparison, not of one run of it.

    It used to be asked for every time and forgotten on exit, which for a window that compares the
    same two databases every day is a question with the same answer every day.
    """

    def test_choosing_one_puts_it_in_the_configuration(self, app, tmp_path):
        app.choose_path = lambda purpose, current: str(tmp_path)
        app.window.choose_output_directory()
        assert app.config.config.output_dir == str(tmp_path)
        assert app.window.output_directory == str(tmp_path)

    def test_choosing_one_marks_the_configuration_unsaved(self, app, tmp_path):
        """Otherwise it is lost on exit and the window never said so."""
        app.choose_path = lambda purpose, current: str(tmp_path)
        app.window.choose_output_directory()
        assert app.config.dirty is True
        assert app.window.dirty is True

    def test_typing_one_puts_it_in_the_configuration(self, app, tmp_path):
        app.window.config_changed("output_dir", str(tmp_path))
        assert app.config.config.output_dir == str(tmp_path)

    def test_clearing_the_field_unsets_it_rather_than_meaning_here(self, app, tmp_path):
        """An empty path would resolve to the config's own directory, which nobody typed."""
        app.window.config_changed("output_dir", str(tmp_path))
        app.window.config_changed("output_dir", "")
        assert app.config.config.output_dir is None
        assert app.window.status_is_error is False

    def test_it_is_written_to_the_file(self, app, tmp_path):
        app.window.config_changed("output_dir", str(tmp_path))
        app.window.save_config()
        assert app.window.status_is_error is False
        assert f"output_dir: {tmp_path}" in app.config.path.read_text()

    def test_a_configuration_that_never_set_one_does_not_write_the_key(self, app):
        """A file that says nothing about reports must stay exactly as it was."""
        app.window.save_config()
        assert "output_dir" not in app.config.path.read_text()

    def test_it_comes_back_when_the_application_restarts(self, tmp_path):
        from db_schema_diff.gui import home

        home.default_path().parent.mkdir(parents=True, exist_ok=True)
        home.default_path().write_text(CONFIG + f"output_dir: {tmp_path}\n")
        app = Application(environ=dict(BOTH), keychain=FakeKeychain())
        assert app.window.output_directory == str(tmp_path)
        assert app.config.config.output_dir == str(tmp_path)

    def test_typing_a_path_one_character_at_a_time_is_not_fought(self, app, tmp_path):
        """Every keystroke updates the configuration, and the refresh that follows writes the
        field back from it — so the two must never disagree about what was typed."""
        wanted = str(tmp_path / "reports")
        for length in range(1, len(wanted) + 1):
            typed = wanted[:length]
            app.window.config_changed("output_dir", typed)
            assert app.window.output_directory == typed
        assert app.config.config.output_dir == wanted

    def test_a_trailing_space_is_not_taken_as_part_of_the_path(self, app, tmp_path):
        """And the space stays in the widget, so typing it is not undone mid-path."""
        app.window.config_changed("output_dir", f"{tmp_path} ")
        assert app.config.config.output_dir == str(tmp_path)

    def test_a_relative_path_resolves_against_the_configuration(self, app, tmp_path):
        """The same meaning as on the command line: relative to the config that named it."""
        (app.config.path.parent / "reports").mkdir()
        app.window.config_changed("output_dir", "reports")
        assert app._output_directory() == app.config.path.parent / "reports"

    def test_saving_somewhere_else_keeps_a_relative_path_pointing_where_it_pointed(
        self, app, tmp_path
    ):
        """It names a directory beside the old config, and has to go on naming that one.

        Asserted by resolving it rather than by comparing the written string: what matters is the
        directory it reaches, not the spelling of the hops to get there.
        """
        from db_schema_diff.config.loader import resolve_output_dir

        reports = app.config.path.parent / "reports"
        reports.mkdir()
        app.window.config_changed("output_dir", "reports")
        before = resolve_output_dir(app.config.config, app.config.path)

        elsewhere = tmp_path / "moved"
        elsewhere.mkdir()
        app.config.save(elsewhere / "invoicing.yaml")

        after = resolve_output_dir(app.config.config, app.config.path)
        assert after is not None and before is not None
        assert after.resolve() == before.resolve() == reports.resolve()
        assert not Path(app.config.config.output_dir).is_absolute(), "it should stay relative"

    def test_saving_somewhere_else_leaves_an_absolute_path_alone(self, app, tmp_path):
        """It was never pointing anywhere near the configuration."""
        target = tmp_path / "fixed"
        target.mkdir()
        app.window.config_changed("output_dir", str(target))
        elsewhere = tmp_path / "moved"
        elsewhere.mkdir()
        app.config.save(elsewhere / "invoicing.yaml")
        assert app.config.config.output_dir == str(target)


class TestTheStoredCredentialFollowsTheFields:
    """Reported: a comparison that reads zero objects from a database full of them.

    Storing a password froze the whole connection string — host, port, database, user — into the
    keychain, and every run connected with that. Editing a field afterwards changed the config
    file and the run dialog and nothing else, so the window showed one database and compared
    another. Reachable and empty reads as a clean capture of nothing.
    """

    def stored(self, app, label: str = "prod") -> str:
        return app.credentials.resolve(app._source(label).dsn_env).value

    def connected(self, app, **parts) -> Application:
        for field, value in parts.items():
            app.window.source_changed("prod", field, value)
        app.window.store_password("prod", "hunter2")
        return app

    def test_changing_the_host_changes_what_a_run_connects_to(self, tmp_path):
        app = application(tmp_path, environ={}, keychain=FakeKeychain())
        self.connected(app, host="db-prod.internal", database="invoicing", user="app")
        assert "db-prod.internal" in self.stored(app)

        app.window.source_changed("prod", "host", "localhost")
        assert "localhost" in self.stored(app)
        assert "db-prod.internal" not in self.stored(app)

    @pytest.mark.parametrize(
        ("field", "value", "expected"),
        [
            ("host", "localhost", "localhost"),
            ("port", "55432", ":55432/"),
            ("database", "core", "/core"),
            ("user", "postgres", "postgres:"),
            ("sslmode", "disable", "sslmode=disable"),
        ],
    )
    def test_every_part_of_the_connection_is_followed(self, tmp_path, field, value, expected):
        app = application(tmp_path, environ={}, keychain=FakeKeychain())
        self.connected(app, host="db-prod.internal", database="invoicing", user="app")
        app.window.source_changed("prod", field, value)
        assert expected in self.stored(app)

    def test_the_password_survives_the_rebuild(self, tmp_path):
        """It cannot be retyped — nobody can read it back — so losing it here would be fatal."""
        app = application(tmp_path, environ={}, keychain=FakeKeychain())
        self.connected(app, host="db-prod.internal", database="invoicing", user="app")
        app.window.source_changed("prod", "host", "localhost")
        assert "hunter2" in self.stored(app)
        assert app.window.sources[0]["has_password"] is True

    def test_a_password_with_punctuation_survives_it_too(self, tmp_path):
        """The rebuild parses the old string and writes a new one, so the escaping round-trips."""
        app = application(tmp_path, environ={}, keychain=FakeKeychain())
        app.window.source_changed("prod", "host", "db-prod")
        app.window.source_changed("prod", "database", "invoicing")
        app.window.source_changed("prod", "user", "app")
        app.window.store_password("prod", "p@ss:w/rd #1")
        app.window.source_changed("prod", "host", "localhost")

        from psycopg.conninfo import conninfo_to_dict

        assert conninfo_to_dict(self.stored(app))["password"] == "p@ss:w/rd #1"

    def test_a_source_with_no_stored_password_gets_none_invented(self, tmp_path):
        keychain = FakeKeychain()
        app = application(tmp_path, environ={}, keychain=keychain)
        app.window.source_changed("prod", "host", "localhost")
        assert keychain.entries == {}

    def test_a_credential_from_the_environment_is_not_copied_into_the_keychain(self, tmp_path):
        """The variable is the user's own. Shadowing it would make the window the authority on a
        credential the command line is still reading from the environment."""
        keychain = FakeKeychain()
        app = application(tmp_path, environ={"PROD_DSN": PROD_DSN}, keychain=keychain)
        app.window.source_changed("prod", "host", "localhost")
        assert keychain.entries == {}
        assert app.credentials.resolve("PROD_DSN").value == PROD_DSN

    def test_editing_an_unrelated_field_does_not_touch_the_credential(self, tmp_path):
        app = application(tmp_path, environ={}, keychain=FakeKeychain())
        self.connected(app, host="db-prod.internal", database="invoicing", user="app")
        before = self.stored(app)
        app.window.source_changed("prod", "schemas", "public")
        assert self.stored(app) == before

    def test_a_rename_keeps_the_connection_it_had(self, tmp_path):
        """The variable moves with the label; the connection it describes does not change."""
        app = application(tmp_path, environ={}, keychain=FakeKeychain())
        self.connected(app, host="db-prod.internal", database="invoicing", user="app")
        before = self.stored(app)
        app.window.source_changed("prod", "label", "production")
        assert self.stored(app, "production") == before

    def test_a_port_is_kept_as_a_number(self, tmp_path):
        """``model_copy`` does not validate, so a typed port arrived as a string and was written
        to the YAML quoted."""
        app = application(tmp_path, environ={}, keychain=FakeKeychain())
        app.window.source_changed("prod", "port", "55432")
        assert app.config.config.master.port == 55432
        assert isinstance(app.config.config.master.port, int)

    def test_clearing_the_port_unsets_it(self, tmp_path):
        app = application(tmp_path, environ={}, keychain=FakeKeychain())
        app.window.source_changed("prod", "port", "55432")
        app.window.source_changed("prod", "port", "")
        assert app.config.config.master.port is None

    def test_a_port_that_is_not_a_number_is_refused_rather_than_stored(self, tmp_path):
        app = application(tmp_path, environ={}, keychain=FakeKeychain())
        app.window.source_changed("prod", "port", "fifty")
        assert app.window.status_is_error is True
        assert app.config.config.master.port is None


class TestReopeningTheSavedConfiguration:
    """The one file the application owns is read back when it starts.

    It used to start empty every time, which was right while it had no fixed place to save to.
    Now that it saves to one known path, not reading that path back meant the only way to see
    yesterday's configuration was to open the YAML in an editor.
    """

    def saved(self, text: str = CONFIG) -> object:
        """Put a configuration where the application saves, before it starts."""
        from db_schema_diff.gui import home

        path = home.default_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def started(self, **kwargs) -> Application:
        return Application(environ=dict(BOTH), keychain=FakeKeychain(), **kwargs)

    def test_the_saved_configuration_is_what_the_window_shows(self):
        self.saved()
        app = self.started()
        assert [row["label"] for row in rows(app.window.sources)] == ["prod", "qa"]
        assert app.window.status_is_error is False

    def test_it_is_adopted_so_saving_goes_back_to_the_same_file(self):
        from db_schema_diff.gui import home

        path = self.saved()
        app = self.started()
        assert app.config.path == path
        app.window.source_changed("qa", "host", "db-qa.internal")
        app.window.save_config()
        assert app.config.path == path
        assert "host: db-qa.internal" in home.default_path().read_text()

    def test_reading_it_is_not_an_unsaved_change(self):
        """Otherwise the window claims there is something to save before anything was touched."""
        self.saved()
        app = self.started()
        assert app.config.dirty is False
        assert app.window.dirty is False

    def test_no_file_still_starts_empty_and_says_nothing(self):
        app = self.started()
        assert [row["label"] for row in rows(app.window.sources)] == ["prod", "qa"]
        assert app.window.status_message == ""

    def test_the_names_in_the_file_are_what_the_keychain_is_asked_for(self):
        """The file carries ``dsn_env``, so a password stored under that name has to be found."""
        self.saved()
        app = Application(environ={}, keychain=FakeKeychain(QA_DSN=QA_DSN))
        by_label_ = {row["label"]: row for row in rows(app.window.sources)}
        assert by_label_["qa"]["has_password"] is True
        assert by_label_["prod"]["has_password"] is False

    def test_a_hand_edited_file_is_told_that_saving_drops_its_comments(self):
        """Saving rewrites the file from the configuration rather than patching it. Said on the
        way in, while the comments are still there to rescue."""
        self.saved("# the invoicing service, by hand\n" + CONFIG)
        app = self.started()
        assert app.window.status_is_error is False
        assert "comments" in app.window.status_message
        assert "Save will not keep" in app.window.status_message
        # The path cannot be what matched: the message names the file, not its directory.
        assert "config.yaml" in app.window.status_message

    def test_a_file_with_no_comments_is_not_warned_about(self):
        self.saved()
        app = self.started()
        assert app.window.status_message == ""

    def test_an_indented_comment_counts(self):
        self.saved(CONFIG + "    # trailing thought\n")
        app = self.started()
        assert "comments" in app.window.status_message

    def test_a_broken_file_leaves_a_window_that_works(self):
        """Refusing to start would leave no way to recover from inside the application."""
        self.saved("version: 1\nmaster: [this is not a source]\n")
        app = self.started()
        assert app.window.status_is_error is True
        assert [row["label"] for row in rows(app.window.sources)] == ["prod", "qa"]
        # And the window is usable, not merely rendered.
        app.window.source_changed("qa", "host", "db-qa.internal")
        assert app.window.status_is_error is False

    def test_a_file_that_is_not_yaml_at_all_is_reported_the_same_way(self):
        self.saved("}{ not yaml\n")
        app = self.started()
        assert app.window.status_is_error is True
        assert "config.yaml" in app.window.status_message

    def test_a_broken_file_can_be_replaced_by_saving_over_it(self):
        """The accepted trade-off: an empty window plus Save is how you get out of a bad file."""
        self.saved("}{ not yaml\n")
        app = self.started()
        app.window.save_config()
        assert app.window.status_is_error is False
        from db_schema_diff.gui import home

        assert "version: 1" in home.default_path().read_text()

    def test_a_secret_in_a_broken_file_is_not_quoted_back(self):
        """A parse error names what it choked on, and someone may have pasted a DSN into the file.

        The message goes to a toast that is also written to the log, so this is the one assertion
        worth repeating at every layer that handles a message.
        """
        self.saved(f"version: 1\nname: [{QA_DSN}\n")
        app = self.started()
        assert app.window.status_is_error is True
        assert SECRET not in app.window.status_message
        assert "postgresql://" not in app.window.status_message

    def test_a_directory_where_the_file_should_be_is_not_a_crash(self):
        """``~/.db_schema_diff/config.yaml`` as a directory is somebody's bad `mkdir`."""
        from db_schema_diff.gui import home

        home.default_path().mkdir(parents=True)
        app = self.started()
        assert [row["label"] for row in rows(app.window.sources)] == ["prod", "qa"]
        assert app.window.status_is_error is True


class TestTheApplicationsOwnFolder:
    """Where a configuration goes when it has none of its own, and what opens with no argument."""

    def home(self):
        from db_schema_diff.gui import home

        return home

    def test_a_new_configuration_saves_into_the_folder(self, app):
        """Before this, a config made here could not be saved at all: `save` wants a path."""
        app.config = app.config.__class__.blank("invoicing")
        app.window.save_config()

        expected = self.home().directory() / "config.yaml"
        assert app.config.path == expected
        assert expected.is_file()
        assert app.window.status_is_error is False

    def test_an_opened_configuration_still_saves_where_it_came_from(self, app, tmp_path):
        """A repository's config belongs to the repository. This is a default, not a destination."""
        elsewhere = tmp_path / "repo" / "invoicing.yaml"
        elsewhere.parent.mkdir()
        app.config.save(elsewhere)

        app.window.source_changed("qa", "host", "db-qa.internal")
        app.window.save_config()

        assert app.config.path == elsewhere
        assert not (self.home().directory() / "config.yaml").exists()

    def test_the_folder_is_made_when_it_is_missing(self, app, monkeypatch, tmp_path):
        target = tmp_path / "not-yet"
        monkeypatch.setenv(self.home().HOME_VARIABLE, str(target))
        app.config = app.config.__class__.blank("invoicing")

        app.window.save_config()

        assert (target / "config.yaml").is_file()

    def test_a_folder_that_cannot_be_made_is_reported_rather_than_crashing(
        self, app, monkeypatch, tmp_path
    ):
        blocked = tmp_path / "a-file"
        blocked.write_text("not a directory")
        monkeypatch.setenv(self.home().HOME_VARIABLE, str(blocked / "home"))
        app.config = app.config.__class__.blank("invoicing")

        app.window.save_config()

        assert app.window.status_is_error is True
        assert "cannot create" in app.window.status_message


class TestWhatOpensOnStartUp:
    """One file, the one it saves to. Nothing else in that folder, and nothing from argv."""

    def start(self, arguments):
        """Returns the configuration path the window ended up with, which should always be none."""
        built: list[Application] = []
        original = Application.__init__

        def remember(self, *args, **kwargs):
            original(self, *args, **kwargs)
            built.append(self)

        with (
            mock.patch.object(app_module.slint, "run_event_loop", lambda coro: coro.close()),
            mock.patch.object(Application, "__init__", remember),
        ):
            assert app_module.run(arguments) == 0
        return [app.config.path for app in built if app.config.path is not None]

    def test_the_folder_is_created_before_anything_looks_in_it(self, monkeypatch, tmp_path):
        """It is where the configuration goes and where the reopen looks, so it has to exist."""
        from db_schema_diff.gui import home

        target = tmp_path / "fresh"
        monkeypatch.setenv(home.HOME_VARIABLE, str(target))
        self.start([])
        assert target.is_dir()

    def test_another_yaml_in_the_folder_is_not_opened(self, monkeypatch, tmp_path):
        """Only ``config.yaml`` is read. There is no chooser, so a second file is not a candidate
        — picking one by name or by mtime would make the window's contents a guess."""
        from db_schema_diff.gui import home

        monkeypatch.setenv(home.HOME_VARIABLE, str(tmp_path))
        (tmp_path / "invoicing.yaml").write_text("version: 1\nname: x\n")
        assert self.start([]) == []

    def test_the_saved_configuration_is_opened(self, monkeypatch, tmp_path):
        from db_schema_diff.gui import home

        monkeypatch.setenv(home.HOME_VARIABLE, str(tmp_path))
        saved = tmp_path / home.CONFIG_NAME
        saved.write_text(CONFIG)
        assert self.start([]) == [saved]


class TestTheAboutView:
    """What it says has to be true, which means none of it is written into the markup."""

    def test_the_version_is_the_installed_one(self, app):
        from db_schema_diff import __version__

        assert app.window.about_version == __version__
        assert app.window.about_version != ""

    def test_the_python_version_is_this_interpreter(self, app):
        assert app.window.about_python.startswith(
            ".".join(str(part) for part in sys.version_info[:2])
        )

    def test_it_names_the_slint_it_is_running_on(self, app):
        import slint

        assert app.window.about_slint == str(getattr(slint, "__version__", "unknown"))

    def test_it_names_the_file_it_writes(self, app):
        """The whole path: it is the one file read and written, and the only thing to point an
        editor at when a configuration needs hand-editing."""
        from db_schema_diff.gui import home

        assert app.window.about_config_path == str(home.default_path())
        assert app.window.about_config_path.endswith("config.yaml")

    def test_it_reports_whether_tunnelling_is_installed(self, app):
        from db_schema_diff.runner import tunnelling_supported

        assert app.window.about_tunnelling is tunnelling_supported()

    def test_no_credential_reaches_it(self, app):
        """It is static text, but it is still a surface, and every surface gets checked."""
        for name in ("about_version", "about_python", "about_slint", "about_config_path"):
            assert SECRET not in str(getattr(app.window, name))


class TestTheTunnelDialog:
    """Modal, and filled in as the test runs rather than all at once when it finishes."""

    def steps(self, app):
        return [dict(row) for row in app.window.tunnel_steps]

    def test_it_opens_before_anything_has_happened(self, app):
        """A gateway can take seconds to answer. A window that shows nothing until then looks
        like a window that has stopped."""
        app.window.source_changed("qa", "ssh_host", "bastion.internal")

        with mock.patch("db_schema_diff.runner.check_tunnel", return_value=UNFINISHED):
            app.window.drive_test_tunnel("qa")

        assert app.window.tunnel_dialog_open() is True
        assert app.window.tunnel_busy is True
        assert app.window.tunnel_summary == ""
        assert [s["state"] for s in self.steps(app)] == ["pending"] * 3

    def test_it_lays_out_every_step_up_front(self, app):
        app.window.source_changed("qa", "ssh_host", "bastion.internal")
        with mock.patch("db_schema_diff.runner.check_tunnel", return_value=UNFINISHED):
            app.window.drive_test_tunnel("qa")

        labels = [s["label"] for s in self.steps(app)]
        assert labels == ["Key", "Gateway", "Channel to the database"]
        assert app.window.tunnel_step_count() == 3

    def test_it_names_the_gateway_it_is_testing(self, app):
        app.window.source_changed("qa", "ssh_host", "bastion.internal")
        app.window.source_changed("qa", "ssh_user", "deploy")
        with mock.patch("db_schema_diff.runner.check_tunnel", return_value=UNFINISHED):
            app.window.drive_test_tunnel("qa")

        assert "bastion.internal" in app.window.tunnel_gateway
        assert "deploy" in app.window.tunnel_gateway

    @pytest.mark.asyncio
    async def test_the_steps_fill_in_as_the_probe_reports(self, app):

        app.window.source_changed("qa", "ssh_host", "bastion.internal")

        def report(source, dsn, *, ssh_passphrase=None, observer=None):
            observer("key", "ok", "~/.ssh/id_ed25519")
            observer("gateway", "running", "connecting")
            observer("gateway", "ok", "host key accepted")
            observer("database", "skipped", "no database address known")
            return TunnelStatus(label="qa", ok=True, detail="reached and authenticated")

        with mock.patch("db_schema_diff.runner.check_tunnel", side_effect=report):
            app.window.drive_test_tunnel("qa")
            await app.wait_for_idle()

        states = {s["label"]: s["state"] for s in self.steps(app)}
        assert states == {"Key": "ok", "Gateway": "ok", "Channel to the database": "skipped"}
        assert "id_ed25519" in self.steps(app)[0]["detail"]

    @pytest.mark.asyncio
    async def test_a_failed_step_is_marked_and_keeps_its_reason(self, app):

        app.window.source_changed("qa", "ssh_host", "bastion.internal")

        def report(source, dsn, *, ssh_passphrase=None, observer=None):
            observer("key", "ok", "")
            observer("gateway", "failed", "authentication was refused")
            return TunnelStatus(label="qa", ok=False, detail="authentication was refused")

        with mock.patch("db_schema_diff.runner.check_tunnel", side_effect=report):
            app.window.drive_test_tunnel("qa")
            await app.wait_for_idle()

        gateway = next(s for s in self.steps(app) if s["label"] == "Gateway")
        assert gateway["state"] == "failed"
        assert "refused" in gateway["detail"]
        assert app.window.tunnel_ok is False

    @pytest.mark.asyncio
    async def test_it_stays_open_until_it_is_closed(self, app):

        app.window.source_changed("qa", "ssh_host", "bastion.internal")
        answer = TunnelStatus(label="qa", ok=True, detail="reached")
        with mock.patch("db_schema_diff.runner.check_tunnel", return_value=answer):
            app.window.drive_test_tunnel("qa")
            await app.wait_for_idle()

        assert app.window.tunnel_dialog_open() is True
        app.window.drive_close_tunnel_dialog()
        assert app.window.tunnel_dialog_open() is False

    @pytest.mark.asyncio
    async def test_it_cannot_be_closed_while_it_is_still_testing(self, app):
        """The Close button is disabled until the answer is in; the state behind it says so."""
        app.window.source_changed("qa", "ssh_host", "bastion.internal")
        with mock.patch("db_schema_diff.runner.check_tunnel", return_value=UNFINISHED):
            app.window.drive_test_tunnel("qa")
            assert app.window.tunnel_busy is True
            await app.wait_for_idle()
        assert app.window.tunnel_busy is False

    @pytest.mark.asyncio
    async def test_an_unexpected_failure_still_releases_the_dialog(self, app):
        """Otherwise the Close button stays disabled and the window is stuck behind a scrim."""
        app.window.source_changed("qa", "ssh_host", "bastion.internal")
        with mock.patch("db_schema_diff.runner.check_tunnel", side_effect=RuntimeError("boom")):
            app.window.drive_test_tunnel("qa")
            await app.wait_for_idle()

        assert app.window.tunnel_busy is False
        assert app.window.tunnel_ok is False
        assert "RuntimeError" in app.window.tunnel_summary
        assert "boom" not in app.window.tunnel_summary
