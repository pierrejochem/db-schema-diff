"""The credential type must be unable to leak, even through a careless f-string."""

import logging

import pytest

from cumo_schema_comparer.config.secrets import resolve_dsn
from cumo_schema_comparer.errors import MissingCredentialsError
from cumo_schema_comparer.logging_setup import RedactingFilter

SECRET = "sup3rs3cret"
URI = f"postgresql://cumo:{SECRET}@db-prod.example:5432/invoicing?sslmode=require"


@pytest.fixture
def dsn(monkeypatch):
    monkeypatch.setenv("PROD_DSN", URI)
    return resolve_dsn("PROD_DSN", role="master 'prod'")


def test_every_string_conversion_redacts(dsn):
    # repr, str and format are the three ways a value reaches a log line by accident.
    assert SECRET not in repr(dsn)
    assert SECRET not in str(dsn)
    assert SECRET not in f"{dsn}"
    assert SECRET not in "{}".format(dsn)  # noqa: UP032
    assert SECRET not in f"{dsn!r}"
    assert SECRET not in f"{dsn!s}"


def test_redacted_form_names_the_env_var_so_it_is_still_diagnosable(dsn):
    assert "PROD_DSN" in str(dsn)


def test_value_is_the_only_accessor_that_returns_the_secret(dsn):
    assert dsn.value == URI


def test_safe_summary_keeps_the_useful_parts_and_drops_the_password(dsn):
    summary = dsn.safe_summary()
    assert summary["host"] == "db-prod.example"
    assert summary["port"] == "5432"
    assert summary["dbname"] == "invoicing"
    assert summary["user"] == "cumo"
    assert "password" not in summary
    assert SECRET not in str(summary)


def test_safe_summary_handles_a_keyword_conninfo_string(monkeypatch):
    monkeypatch.setenv(
        "KW_DSN", f"host=db-qa port=5433 dbname=invoicing user=cumo password={SECRET}"
    )
    summary = resolve_dsn("KW_DSN", role="target 'qa'").safe_summary()
    assert summary["host"] == "db-qa"
    assert summary["dbname"] == "invoicing"
    assert "password" not in summary


def test_safe_summary_never_raises_on_an_unparseable_value(monkeypatch):
    monkeypatch.setenv("BAD_DSN", "this is not a conninfo string")
    summary = resolve_dsn("BAD_DSN", role="target 'x'").safe_summary()
    assert summary == {"source": "$BAD_DSN", "parsed": "unparseable"}


@pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
def test_blank_counts_as_missing(monkeypatch, raw):
    monkeypatch.setenv("EMPTY_DSN", raw)
    with pytest.raises(MissingCredentialsError):
        resolve_dsn("EMPTY_DSN", role="master 'prod'")


def test_unset_counts_as_missing(monkeypatch):
    monkeypatch.delenv("NOPE_DSN", raising=False)
    with pytest.raises(MissingCredentialsError) as exc:
        resolve_dsn("NOPE_DSN", role="master 'prod'")
    assert "NOPE_DSN" in str(exc.value)
    assert "master 'prod'" in str(exc.value)


def test_value_is_stripped_of_surrounding_whitespace(monkeypatch):
    monkeypatch.setenv("PAD_DSN", f"  {URI}\n")
    assert resolve_dsn("PAD_DSN", role="master 'prod'").value == URI


def test_dsn_is_not_iterable_or_subscriptable(dsn):
    # Guards against a caller accidentally treating it as the raw string.
    with pytest.raises(TypeError):
        _ = dsn[0]
    with pytest.raises(TypeError):
        list(dsn)


class TestRedactingFilter:
    def _record(self, msg, *args):
        return logging.LogRecord("t", logging.INFO, __file__, 1, msg, args, None)

    def test_scrubs_a_registered_secret_from_the_message(self, dsn):
        flt = RedactingFilter([dsn])
        record = self._record("connect failed for %s", URI)
        flt.filter(record)
        assert SECRET not in record.getMessage()
        assert "***" in record.getMessage()

    def test_scrubs_the_password_component_on_its_own(self, dsn):
        flt = RedactingFilter([dsn])
        record = self._record("libpq said: password=%s rejected", SECRET)
        flt.filter(record)
        assert SECRET not in record.getMessage()

    def test_leaves_unrelated_messages_untouched(self, dsn):
        flt = RedactingFilter([dsn])
        record = self._record("comparing %d schemas", 4)
        flt.filter(record)
        assert record.getMessage() == "comparing 4 schemas"


def test_redacting_filter_add_keeps_earlier_secrets(monkeypatch):
    monkeypatch.setenv("A_DSN", "postgresql://u:first_secret@a/db")
    monkeypatch.setenv("B_DSN", "postgresql://u:second_secret@b/db")
    first = resolve_dsn("A_DSN", role="master 'a'")
    second = resolve_dsn("B_DSN", role="target 'b'")

    flt = RedactingFilter([first])
    flt.add(second)

    record = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "%s and %s", (first.value, second.value), None
    )
    flt.filter(record)
    assert "first_secret" not in record.getMessage()
    assert "second_secret" not in record.getMessage()
