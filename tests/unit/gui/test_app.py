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
import textwrap
import threading
import time
from typing import Any
from unittest import mock

import pytest

pytest.importorskip("slint", reason="the GUI extra is not installed")

from cumo_schema_comparer.errors import ProbeError
from cumo_schema_comparer.gui import app as app_module
from cumo_schema_comparer.gui.app import Application
from cumo_schema_comparer.gui.session import Session
from cumo_schema_comparer.runner import CaptureResult, ConnectionStatus
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
CAPTURE = "cumo_schema_comparer.gui.session.runner.capture"
CHECK = "cumo_schema_comparer.gui.session.runner.check_connection"
BUILD_REPORT = "cumo_schema_comparer.gui.session.runner.build_report"

SOURCE_ROW_FIELDS = {
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
CREDENTIAL_ROW_FIELDS = {"env_name", "used_by", "source", "summary", "can_store"}
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


def application(tmp_path, *, environ=None, keychain=None, text: str = CONFIG) -> Application:
    app = Application(
        environ=dict(BOTH) if environ is None else environ,
        keychain=FakeKeychain() if keychain is None else keychain,
    )
    app.open_config(write_config(tmp_path, text))
    return app


@pytest.fixture
def app(tmp_path) -> Application:
    return application(tmp_path)


def rows(model) -> list[dict[str, Any]]:
    return [dict(row) for row in model]


def by_env(app: Application) -> dict[str, dict[str, Any]]:
    return {row["env_name"]: row for row in rows(app.window.credentials)}


def everything_rendered(app: Application) -> str:
    """Every string the window holds, for the one assertion worth repeating at every layer."""
    window = app.window
    return str(
        [
            rows(window.sources),
            rows(window.credentials),
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
        assert app.window.config_name == "invoicing"
        assert [row["label"] for row in app.window.sources] == ["prod", "qa"]

    def test_the_window_shows_every_option(self, app):
        assert app.window.max_workers == 4
        assert app.window.fail_on == "error"
        assert app.window.parallel is True

    def test_credentials_show_their_source(self, tmp_path):
        app = application(tmp_path, environ={"PROD_DSN": PROD_DSN})
        assert by_env(app)["PROD_DSN"]["source"] == "environment"
        assert by_env(app)["QA_DSN"]["source"] == "unset"

    def test_no_credential_value_reaches_the_window(self, app):
        # The one assertion worth repeating at every layer.
        assert "postgresql://" not in str(rows(app.window.credentials))
        assert "postgresql://" not in str(rows(app.window.sources))

    def test_every_source_row_carries_all_thirteen_fields(self, app):
        for row in rows(app.window.sources):
            assert set(row) == SOURCE_ROW_FIELDS

    def test_every_credential_row_carries_all_five_fields(self, app):
        for row in rows(app.window.credentials):
            assert set(row) == CREDENTIAL_ROW_FIELDS

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

    def test_a_directory_becomes_a_status_message(self, tmp_path):
        app = Application(environ={}, keychain=FakeKeychain())
        app.open_config(tmp_path)
        assert app.window.status_is_error is True
        assert "Traceback" not in app.window.status_message

    def test_an_unreadable_ignores_file_still_leaves_a_usable_window(self, tmp_path):
        app = application(tmp_path, text=CONFIG + "ignores_file: ../nowhere/rules.yaml\n")
        # Declared but absent is allowed (the tab can create it); a broken one must not crash.
        assert app.ignores is not None
        assert app.window.config_name == "invoicing"


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
        app.window.source_changed("", "max_workers", "9")
        app.window.source_changed("", "parallel", "false")
        app.window.source_changed("", "fail_on", "warning")
        assert app.config.config.options.max_workers == 9
        assert app.config.config.options.parallel is False
        assert app.config.config.options.fail_on == "warning"
        assert app.window.max_workers == 9

    def test_an_out_of_range_option_is_reported_by_validate_rather_than_crashing(self, app):
        app.window.source_changed("", "max_workers", "999")
        app.window.validate_config()
        assert app.window.status_is_error is True
        assert "max_workers" in app.window.status_message

    def test_a_non_numeric_option_becomes_a_status_message(self, app):
        app.window.source_changed("", "connect_timeout_seconds", "soon")
        assert app.window.status_is_error is True
        assert app.config.config.options.connect_timeout_seconds == 10

    def test_exclude_schemas_round_trips_through_the_text_field(self, app):
        app.window.source_changed("", "exclude_schemas", "quartz, audit_archive")
        assert app.config.config.exclude_schemas == ("quartz", "audit_archive")
        assert list(app.window.exclude_schemas) == ["quartz", "audit_archive"]

    def test_a_half_typed_schema_list_is_not_rewritten_under_the_cursor(self, app):
        app.window.exclude_schemas_text = "quartz, "
        app.window.source_changed("", "exclude_schemas", "quartz, ")
        assert app.window.exclude_schemas_text == "quartz, "

    def test_schema_map_is_parsed_into_pairs(self, app):
        app.window.source_changed("qa", "schema_map", "cumo-invoicing=inv_qa")
        assert app.config.config.targets[0].schema_map == {"cumo-invoicing": "inv_qa"}
        assert rows(app.window.sources)[1]["schema_map"] == "cumo-invoicing=inv_qa"

    def test_a_malformed_schema_map_becomes_a_status_message(self, app):
        app.window.source_changed("qa", "schema_map", "cumo-invoicing")
        assert app.window.status_is_error is True
        assert app.config.config.targets[0].schema_map == {}

    def test_the_liquibase_location_is_two_fields_over_one_model(self, app):
        app.window.source_changed("qa", "liquibase_schema", "cumo-invoicing")
        app.window.source_changed("qa", "liquibase_table", "DBCHANGELOG")
        liquibase = app.config.config.targets[0].liquibase
        assert (liquibase.schema_name, liquibase.table) == ("cumo-invoicing", "DBCHANGELOG")
        row = rows(app.window.sources)[1]
        assert (row["liquibase_schema"], row["liquibase_table"]) == (
            "cumo-invoicing",
            "DBCHANGELOG",
        )

    def test_a_liquibase_table_without_a_schema_is_refused(self, app):
        app.window.source_changed("qa", "liquibase_table", "DBCHANGELOG")
        assert app.window.status_is_error is True
        assert app.config.config.targets[0].liquibase is None

    def test_clearing_both_liquibase_fields_clears_the_reference(self, app):
        app.window.source_changed("qa", "liquibase_schema", "cumo-invoicing")
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

    def test_a_file_with_comments_says_they_were_dropped(self, tmp_path):
        app = application(tmp_path, text="# the invoicing service\n" + CONFIG)
        app.window.save_config()
        assert app.window.status_is_error is False
        assert "comment" in app.window.status_message


class TestCredentials:
    def test_storing_a_credential_updates_the_row_without_showing_it(self, app):
        app.window.store_credential("QA_DSN", QA_DSN)
        assert by_env(app)["QA_DSN"]["source"] == "keychain"
        assert SECRET not in everything_rendered(app)

    def test_forgetting_a_credential_falls_back(self, app):
        app.window.store_credential("PROD_DSN", "postgresql://u:x@kc/db")
        app.window.forget_credential("PROD_DSN")
        assert by_env(app)["PROD_DSN"]["source"] == "environment"

    def test_the_keychain_wins_over_the_environment(self, app):
        app.window.store_credential("PROD_DSN", "postgresql://u:x@kc/other")
        row = by_env(app)["PROD_DSN"]
        assert row["source"] == "keychain"
        assert "host=kc" in row["summary"]

    def test_an_empty_secret_is_refused_with_a_message(self, tmp_path):
        app = application(tmp_path, environ={"PROD_DSN": PROD_DSN})
        app.window.store_credential("QA_DSN", "   ")
        assert app.window.status_is_error is True
        assert by_env(app)["QA_DSN"]["source"] == "unset"

    def test_an_unavailable_keychain_is_explained_rather_than_hidden(self, tmp_path):
        application_ = Application(environ={}, keychain=None)
        application_.open_config(write_config(tmp_path))
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

    def test_storing_a_credential_is_seen_by_the_next_refresh(self, tmp_path):
        app = application(tmp_path, environ={"PROD_DSN": PROD_DSN})
        assert by_env(app)["QA_DSN"]["source"] == "unset"
        app.window.store_credential("QA_DSN", QA_DSN)
        assert by_env(app)["QA_DSN"]["source"] == "keychain"
        app.window.forget_credential("QA_DSN")
        assert by_env(app)["QA_DSN"]["source"] == "unset"

    @pytest.mark.parametrize("shape", SECRET_SHAPES, ids=lambda s: s.split("=", 1)[1][:14])
    def test_a_quoted_or_escaped_password_is_redacted_whole(self, tmp_path, shape):
        """Not merely its first token: a space in the value must not end the redaction."""
        app = application(tmp_path, keychain=EchoingKeychain(shape))

        assert app.window.keychain_available is False
        app.window.store_credential("QA_DSN", "postgresql://u:p@h/db")
        app.window.forget_credential("QA_DSN")

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
        kept = app_module._sanitise("qa: cannot connect (host=db-qa, port=5432, user=cumo)")
        assert kept == "qa: cannot connect (host=db-qa, port=5432, user=cumo)"
        assert app_module._sanitise("refused postgresql://u:p@h/db") == "refused ***"
        assert app_module._sanitise("sslkey='a b' host=db") == "*** host=db"

    def test_a_credential_only_in_the_keychain_is_usable_without_the_environment(self, tmp_path):
        app = application(tmp_path, environ={}, keychain=FakeKeychain(QA_DSN=QA_DSN))
        assert by_env(app)["QA_DSN"]["source"] == "keychain"
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
            app.window.source_changed("", "exclude_schemas", "quartz")
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
        app.window.output_directory = str(tmp_path)
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

    def test_loading_another_config_brings_its_own_gate(self, tmp_path):
        app = application(tmp_path, text=CONFIG + "options:\n  fail_on: never\n")
        app.window.run_fail_on = "any"
        other = tmp_path / "other.yaml"
        other.write_text(CONFIG + "options:\n  fail_on: warning\n")
        app.open_config(other)
        assert app.window.run_fail_on == "warning"
        assert app._effective_config().options.fail_on == "warning"

    def test_editing_the_configs_gate_carries_the_run_tab_with_it(self, tmp_path):
        app = application(tmp_path)
        app.window.source_changed("", "fail_on", "any")
        assert (app.window.fail_on, app.window.run_fail_on) == ("any", "any")
        assert app._effective_config().options.fail_on == "any"

    @pytest.mark.asyncio
    async def test_a_configured_gate_reaches_the_written_report(self, app, tmp_path):
        app.window.source_changed("", "fail_on", "never")
        with mock.patch(CAPTURE, drifted_capture):
            await run_to_completion(app)
        assert "--fail-on never" in app.window.verdict
        app.window.output_directory = str(tmp_path)
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
            user="cumo",
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
    """StatusLine has two colours and is always on screen, so every message picks one.

    A flip in either direction is invisible to a test that only checks the text, and the Task 10
    banner defect was exactly that. These assert the colour, not the words.
    """

    def test_every_routine_notice_is_green(self, app, tmp_path):
        app.choose_path = lambda purpose, current: str(tmp_path)

        def green(what: str) -> None:
            assert app.window.status_is_error is False, f"{what}: {app.window.status_message}"
            assert app.window.status_message != "", f"{what}: no message at all"

        green("loading a config")
        app.window.add_target()
        green("adding a target")
        app.window.remove_target("target")
        green("removing a target")
        app.window.add_rule()
        green("adding a rule")
        app.window.remove_rule(rows(app.window.rules)[0]["id"])
        green("removing a rule")
        app.window.store_credential("QA_DSN", QA_DSN)
        green("storing a credential")
        app.window.forget_credential("QA_DSN")
        green("forgetting a credential")
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
        app.window.output_directory = str(tmp_path)
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
        finished.window.output_directory = str(tmp_path)
        finished.window.drive_write_reports()
        assert finished.window.status_is_error is False, finished.window.status_message
        for name in ("report.json", "junit.xml", "report.html"):
            assert (tmp_path / name).exists()

    def test_writing_without_a_directory_asks_for_one(self, finished):
        finished.window.output_directory = ""
        finished.window.drive_write_reports()
        assert finished.window.status_is_error is True

    def test_opening_the_html_report_needs_it_to_exist(self, finished, tmp_path):
        opened: list[str] = []
        finished.open_url = lambda url: bool(opened.append(url))
        finished.window.output_directory = str(tmp_path)
        finished.window.drive_open_html()
        assert finished.window.status_is_error is True
        assert opened == []

        finished.window.drive_write_reports()
        finished.window.drive_open_html()
        assert opened and opened[0].endswith("report.html")

    def test_nothing_can_be_written_before_a_run(self, app, tmp_path):
        app.window.output_directory = str(tmp_path)
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
        from cumo_schema_comparer.model.keys import column_key

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

    def test_run_opens_a_config_named_on_the_command_line(self, tmp_path):
        path = write_config(tmp_path)
        opened: list[Any] = []

        with (
            mock.patch.object(app_module.slint, "run_event_loop", lambda coro: coro.close()),
            mock.patch.object(Application, "open_config", lambda self, p: opened.append(p)),
        ):
            assert app_module.run([str(path)]) == 0
        assert opened == [path]

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
