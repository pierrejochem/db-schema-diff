"""Credential resolution and storage.

Two properties matter more than any feature here: no raw connection string may reach the UI, and a
keychain that is unavailable or refuses access must degrade rather than crash.
"""

from __future__ import annotations

import pytest

from db_schema_diff.config.secrets import Dsn
from db_schema_diff.errors import MissingCredentialsError
from db_schema_diff.gui.credentials import (
    KEYCHAIN_SERVICE,
    CredentialSource,
    CredentialStore,
)

SECRET = "postgresql://app:hunter2@db-prod:5432/invoicing"
OTHER = "postgresql://app:other@db-qa:5432/invoicing"


class FakeKeyring:
    """A keyring backend that records calls and can be made to fail."""

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.fail_with = fail_with

    def get_password(self, service: str, username: str) -> str | None:
        if self.fail_with:
            raise self.fail_with
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        if self.fail_with:
            raise self.fail_with
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if self.fail_with:
            raise self.fail_with
        self.values.pop((service, username), None)


def store(backend=None, **environ) -> CredentialStore:
    return CredentialStore(backend=backend or FakeKeyring(), environ=environ)


class TestPrecedence:
    def test_the_keychain_wins_over_the_environment(self):
        backend = FakeKeyring()
        backend.values[(KEYCHAIN_SERVICE, "PROD_DSN")] = SECRET
        subject = store(backend, PROD_DSN=OTHER)
        assert subject.resolve("PROD_DSN").value == SECRET
        assert subject.describe("PROD_DSN").source is CredentialSource.KEYCHAIN

    def test_the_environment_is_used_when_the_keychain_has_nothing(self):
        subject = store(PROD_DSN=SECRET)
        assert subject.resolve("PROD_DSN").value == SECRET
        assert subject.describe("PROD_DSN").source is CredentialSource.ENVIRONMENT

    def test_neither_is_reported_as_unset(self):
        assert store().describe("PROD_DSN").source is CredentialSource.UNSET

    def test_resolving_an_unset_credential_names_it(self):
        with pytest.raises(MissingCredentialsError, match="PROD_DSN"):
            store().resolve("PROD_DSN")

    def test_a_blank_value_counts_as_unset(self):
        # Matches the CLI, where a blank variable is not a credential.
        assert store(PROD_DSN="   ").describe("PROD_DSN").source is CredentialSource.UNSET


class TestNothingLeaks:
    def test_the_status_summary_never_contains_a_password(self):
        status = store(PROD_DSN=SECRET).describe("PROD_DSN")
        assert status.summary is not None
        assert "hunter2" not in status.summary
        assert "db-prod" in status.summary

    def test_the_status_is_safe_to_render_whole(self):
        status = store(PROD_DSN=SECRET).describe("PROD_DSN")
        assert "hunter2" not in repr(status)
        assert "hunter2" not in str(status)

    def test_resolve_returns_a_self_redacting_dsn(self):
        # Dsn is the only type allowed to cross into the UI layer.
        dsn = store(PROD_DSN=SECRET).resolve("PROD_DSN")
        assert isinstance(dsn, Dsn)
        assert "hunter2" not in f"{dsn}"
        assert "hunter2" not in repr(dsn)

    def test_a_malformed_value_still_produces_a_safe_summary(self):
        status = store(PROD_DSN="this is not a conninfo string").describe("PROD_DSN")
        assert status.summary is not None
        assert "unparseable" in status.summary


class TestStorage:
    def test_storing_then_resolving_round_trips(self):
        backend = FakeKeyring()
        subject = store(backend)
        subject.store("PROD_DSN", SECRET)
        assert subject.resolve("PROD_DSN").value == SECRET
        assert backend.values[(KEYCHAIN_SERVICE, "PROD_DSN")] == SECRET

    def test_forgetting_falls_back_to_the_environment(self):
        backend = FakeKeyring()
        subject = store(backend, PROD_DSN=OTHER)
        subject.store("PROD_DSN", SECRET)
        subject.forget("PROD_DSN")
        assert subject.resolve("PROD_DSN").value == OTHER

    def test_storing_a_blank_value_is_refused(self):
        # Silently storing nothing would look like success and fail at connect time.
        with pytest.raises(ValueError, match="empty"):
            store().store("PROD_DSN", "   ")


class TestUnavailableBackend:
    """Review Focus 1: the keychain can be absent, locked, or refused.

    macOS prompts for permission and the prompt can be denied; a headless Linux machine has no
    backend at all. Neither may crash the application.
    """

    def test_a_refused_keychain_falls_back_to_the_environment(self):
        backend = FakeKeyring(fail_with=RuntimeError("User denied access"))
        subject = store(backend, PROD_DSN=SECRET)
        assert subject.resolve("PROD_DSN").value == SECRET
        assert subject.describe("PROD_DSN").source is CredentialSource.ENVIRONMENT

    def test_a_refused_keychain_is_reported_rather_than_hidden(self):
        backend = FakeKeyring(fail_with=RuntimeError("User denied access"))
        subject = store(backend, PROD_DSN=SECRET)
        assert subject.storage_available is False
        # Reported as a type, not in the backend's own words; see TestBackendTextIsNotRepeated.
        problem = subject.storage_problem or ""
        assert "keychain unavailable" in problem
        assert "RuntimeError" in problem
        assert "denied" not in problem.lower()
        assert subject.describe("PROD_DSN").storage_available is False

    def test_storing_into_an_unavailable_keychain_raises_a_clear_error(self):
        backend = FakeKeyring(fail_with=RuntimeError("no backend"))
        subject = store(backend)
        with pytest.raises(RuntimeError):
            subject.store("PROD_DSN", SECRET)

    def test_a_missing_backend_module_degrades_to_environment_only(self):
        subject = CredentialStore(backend=None, environ={"PROD_DSN": SECRET}, _import_keyring=False)
        assert subject.storage_available is False
        assert subject.resolve("PROD_DSN").value == SECRET


class OddBackend:
    def __init__(self, value: object) -> None:
        self.value = value

    def get_password(self, service: str, username: str) -> object:
        return self.value


class TestFixRound:
    def test_a_blank_keychain_entry_falls_through_to_the_environment(self):
        backend = FakeKeyring()
        backend.values[(KEYCHAIN_SERVICE, "PROD_DSN")] = "   "
        subject = store(backend, PROD_DSN=SECRET)
        assert subject.describe("PROD_DSN").source is CredentialSource.ENVIRONMENT

    def test_a_keychain_entry_wins_over_a_blank_environment_variable(self):
        backend = FakeKeyring()
        backend.values[(KEYCHAIN_SERVICE, "PROD_DSN")] = SECRET
        subject = store(backend, PROD_DSN="  ")
        assert subject.describe("PROD_DSN").source is CredentialSource.KEYCHAIN

    def test_describe_on_a_healthy_keychain_reports_storage_available(self):
        assert store(PROD_DSN=SECRET).describe("PROD_DSN").storage_available is True

    @pytest.mark.parametrize("value", [42, b"postgresql://u:p@h/d", ["x"]])
    def test_a_non_string_from_the_backend_is_treated_as_absent(self, value):
        subject = store(OddBackend(value), PROD_DSN=OTHER)
        assert subject.describe("PROD_DSN").source is CredentialSource.ENVIRONMENT
        assert subject.resolve("PROD_DSN").value == OTHER
        assert "unexpected type" in (subject.storage_problem or "")
        assert subject.storage_available is False

    def test_a_denial_at_write_time_is_reported_and_marks_storage_unavailable(self):
        backend = FakeKeyring()
        subject = store(backend)
        assert subject.storage_available is True
        backend.fail_with = RuntimeError("User denied access")
        with pytest.raises(RuntimeError, match="keychain unavailable"):
            subject.store("PROD_DSN", SECRET)
        assert subject.storage_available is False
        assert "RuntimeError" in (subject.storage_problem or "")

    def test_forget_refuses_when_storage_is_unavailable(self):
        subject = store(FakeKeyring(fail_with=RuntimeError("locked")))
        with pytest.raises(RuntimeError, match="keychain unavailable"):
            subject.forget("PROD_DSN")


class LoudBackend(Exception):
    """A backend error that quotes the value it was handed, the way a real one can.

    macOS's Security framework and Linux's Secret Service both produce messages this tool does not
    control, and a stored credential is exactly what they were given.
    """


class TestBackendTextIsNotRepeated:
    """``store`` and ``forget`` used to put the backend's own message into ``_problem`` and into
    ``__cause__``. ``gui.app._sanitise`` catches a URL and a ``password=`` shape, but not a bare
    echoed value, and a chained cause carries the text into every traceback regardless."""

    LOUD = LoudBackend(f"could not write {SECRET}: item hunter2 already exists")

    def each_operation(self):
        for name, call in (
            ("store", lambda s: s.store("PROD_DSN", SECRET)),
            ("forget", lambda s: s.forget("PROD_DSN")),
        ):
            backend = FakeKeyring()
            subject = store(backend)
            assert subject.storage_available is True
            backend.fail_with = self.LOUD
            yield name, subject, call

    def test_neither_the_raised_message_nor_the_problem_repeats_the_backend(self):
        for name, subject, call in self.each_operation():
            with pytest.raises(RuntimeError) as raised:
                call(subject)
            text = str(raised.value) + (subject.storage_problem or "")
            assert "hunter2" not in text, name
            assert SECRET not in text, name
            assert "LoudBackend" in text, name

    def test_the_cause_is_dropped_so_no_traceback_carries_the_text(self):
        for name, subject, call in self.each_operation():
            with pytest.raises(RuntimeError) as raised:
                call(subject)
            assert raised.value.__cause__ is None, name
            assert raised.value.__suppress_context__ is True, name

    def test_a_read_failure_is_type_only_too(self):
        subject = store(FakeKeyring(fail_with=self.LOUD), PROD_DSN=OTHER)
        problem = subject.storage_problem or ""
        assert "hunter2" not in problem and SECRET not in problem
        assert "LoudBackend" in problem
        # And the application still works from the environment.
        assert subject.resolve("PROD_DSN").value == OTHER

    def test_the_log_records_the_type_and_not_the_message(self, caplog):
        import logging

        caplog.set_level(logging.WARNING)
        subject = store(FakeKeyring(fail_with=self.LOUD))
        with pytest.raises(RuntimeError):
            subject.store("PROD_DSN", SECRET)
        assert caplog.text
        assert "hunter2" not in caplog.text and SECRET not in caplog.text
        assert "LoudBackend" in caplog.text
