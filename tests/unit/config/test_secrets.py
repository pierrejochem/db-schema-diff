"""The credential type must be unable to leak, even through a careless f-string."""

import logging
from typing import ClassVar

import pytest

from db_schema_comparer.config.secrets import Dsn, resolve_dsn
from db_schema_comparer.errors import MissingCredentialsError
from db_schema_comparer.logging_setup import RedactingFilter

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


@pytest.mark.parametrize(
    "raw",
    [
        "postgresql://u:zz@secret@h:5432/db",
        "postgresql://u:zz%secret@h:5432/db",
        "postgresql://u:zz secret@h:5432/db",
        "postgresql://u:zz'secret@h:5432/db",
        'postgresql://u:zz"secret@h:5432/db',
        "host=h user=u password='zz@secret'",
        "host=h user=u password='zz secret'",
        'host=h user=u password="zz\'secret"',
        "host=h dbname=d user=u options='-c password=zzsecret'",
        "hostaddr=zz@secret host=h",
    ],
)
def test_safe_summary_leaks_no_password_fragment(monkeypatch, raw):
    monkeypatch.setenv("LEAK_DSN", raw)
    dsn = resolve_dsn("LEAK_DSN", role="target 'x'")
    summary = dsn.safe_summary()
    for text in (str(summary), repr(summary), str(dsn)):
        assert "secret" not in text
        assert "zz" not in text


def test_safe_summary_fails_closed_on_an_at_sign_host(monkeypatch):
    monkeypatch.setenv("AT_DSN", "postgresql://u:zz@secret@h:5432/db")
    summary = resolve_dsn("AT_DSN", role="target 'x'").safe_summary()
    assert summary == {"source": "$AT_DSN", "parsed": "unparseable"}


class TestSecretFragments:
    def _frags(self, raw):
        return Dsn(raw, env_name="X").secret_fragments()

    def test_uri_with_an_unencoded_at(self):
        frags = self._frags("postgresql://u:p@ss@h:5432/db")
        assert "ss" in frags and "u:p@ss" in frags and "p@ss" in frags

    def test_uri_with_two_unencoded_ats(self):
        frags = self._frags("postgresql://u:a@b@c@h:5432/db")
        assert {"a@b@c", "u:a@b@c", "b", "c"} <= set(frags)

    def test_keyword_form_with_a_quoted_password(self):
        frags = self._frags("host=h user=u password='zz sec\\'ret' dbname=d")
        assert "zz sec'ret" in frags

    def test_no_password_yields_an_empty_tuple(self):
        assert self._frags("host=h user=u dbname=d") == ()
        assert self._frags("postgresql://u@h/db") == ()

    def test_longest_first_and_deduplicated(self):
        frags = self._frags("postgresql://u:p@ss@h:5432/db")
        assert list(frags) == sorted(set(frags), key=lambda f: (-len(f), f))
        assert "" not in frags


class TestFailClosed:
    HOSTILE: ClassVar[list[str]] = [
        "p@ss",
        "pa/ss",
        "pa?ss",
        "pa#ss",
        "pa ss",
        "p:ss",
        "pa%2Fss",
        "a/b@c",
    ]

    def test_slash_password_fragments(self):
        frags = Dsn("postgresql://u:pa/ss@nohost.invalid:5432/db", env_name="X").secret_fragments()
        assert {"pa/ss", "pa", "ss"} <= set(frags)

    def test_slash_and_at_password_fragments(self):
        frags = Dsn("postgresql://u:a/b@c@h:5432/db", env_name="X").secret_fragments()
        assert {"a/b@c", "a/b", "c"} <= set(frags)

    def test_question_and_hash_password_fragments(self):
        frags = Dsn("postgresql://u:a?b#c@h:5432/db", env_name="X").secret_fragments()
        assert "a?b#c" in frags

    @pytest.mark.parametrize(
        "raw",
        [
            "postgresql://u:pa/ss@nohost.invalid:5432/db",  # port not digits
            "postgresql://u:p@ss@nohost.invalid:5432/db",  # host contains @
            "postgresql://u:x/y@h/db",  # host contains /
            "host=a:b user=u",  # host contains :
            "host=a\\ b user=u",  # host contains whitespace
            "postgresql://h/d@b:z",  # dbname-ish
        ],
    )
    def test_misparse_fails_closed(self, raw):
        assert Dsn(raw, env_name="X").safe_summary() == {"source": "$X", "parsed": "unparseable"}

    def test_dbname_with_slash_at_or_colon_fails_closed(self):
        from db_schema_comparer.config import secrets

        for bad in ("a@b", "a/b", "a:b"):
            assert secrets._looks_misparsed({"dbname": bad})

    @pytest.mark.parametrize("password", HOSTILE)
    def test_hostile_passwords_never_leak(self, password):
        from db_schema_comparer.db.connect import _failure_message

        dsn = Dsn(f"postgresql://u:{password}@nohost.invalid:5432/db", env_name="X")
        summary = dsn.safe_summary()
        reason = RuntimeError(f"failed to resolve host '{password.split('@')[-1]}@nohost.invalid'")
        texts = [str(summary), str(dsn), _failure_message("t", dsn, reason)]
        pieces = {
            p
            for p in password.replace("%2F", "/")
            .replace("@", " ")
            .replace("/", " ")
            .replace("?", " ")
            .replace("#", " ")
            .replace(":", " ")
            .split()
            if len(p) > 2
        }
        for text in texts:
            assert password not in text
            for piece in pieces:
                assert piece not in text

    def test_a_well_formed_dsn_keeps_the_full_summary(self):
        dsn = Dsn("postgresql://cumo:pw@db-prod:5433/inv?sslmode=require", env_name="X")
        assert dsn.safe_summary() == {
            "host": "db-prod",
            "port": "5433",
            "dbname": "inv",
            "user": "cumo",
            "sslmode": "require",
            "source": "$X",
        }

    def test_ipv6_host_is_still_summarised(self):
        assert Dsn("host=::1 user=u", env_name="X").safe_summary()["host"] == "::1"

    def test_unix_socket_directory_keyword_form(self, monkeypatch):
        """Unix-socket directory (starts with /) is a legitimate host."""
        monkeypatch.setenv("SOCK_DSN", "host=/var/run/postgresql user=u dbname=d")
        summary = resolve_dsn("SOCK_DSN", role="target 'x'").safe_summary()
        assert summary["host"] == "/var/run/postgresql"
        assert summary["dbname"] == "d"
        assert summary["user"] == "u"

    def test_unix_socket_directory_url_encoded(self, monkeypatch):
        """Unix-socket directory encoded in URI form."""
        monkeypatch.setenv("SOCK_URI_DSN", "postgresql://u:pw@%2Fvar%2Frun%2Fpostgresql/db")
        summary = resolve_dsn("SOCK_URI_DSN", role="target 'x'").safe_summary()
        assert summary["host"] == "/var/run/postgresql"
        assert summary["dbname"] == "db"
        assert summary["user"] == "u"

    def test_comma_separated_hosts_and_ports(self, monkeypatch):
        """libpq accepts comma-separated host and port lists."""
        monkeypatch.setenv("MULTI_HOST_DSN", "host=h1,h2 port=5432,5433 user=u dbname=d")
        summary = resolve_dsn("MULTI_HOST_DSN", role="target 'x'").safe_summary()
        assert summary["host"] == "h1,h2"
        assert summary["port"] == "5432,5433"
        assert summary["dbname"] == "d"
        assert summary["user"] == "u"

    def test_comma_separated_hosts_and_ports_in_uri(self, monkeypatch):
        """libpq accepts comma-separated host:port pairs in URI form."""
        monkeypatch.setenv("MULTI_URI_DSN", "postgresql://u:pw@h1:5432,h2:5433/db")
        summary = resolve_dsn("MULTI_URI_DSN", role="target 'x'").safe_summary()
        assert summary["host"] == "h1,h2"
        assert summary["port"] == "5432,5433"
        assert summary["dbname"] == "db"
        assert summary["user"] == "u"

    def test_slash_in_host_without_leading_slash_fails_closed(self, monkeypatch):
        """A / in a host that doesn't start with / is a misparsed credential."""
        monkeypatch.setenv("BAD_SLASH_HOST_DSN", "postgresql://u:pa/ss@h/db")
        summary = resolve_dsn("BAD_SLASH_HOST_DSN", role="target 'x'").safe_summary()
        assert summary == {"source": "$BAD_SLASH_HOST_DSN", "parsed": "unparseable"}

    def test_non_digit_port_fails_closed(self, monkeypatch):
        """A port with non-digits is a misparsed credential."""
        monkeypatch.setenv("BAD_PORT_DSN", "postgresql://u:pw@h:abcd/db")
        summary = resolve_dsn("BAD_PORT_DSN", role="target 'x'").safe_summary()
        assert summary == {"source": "$BAD_PORT_DSN", "parsed": "unparseable"}

    def test_comma_port_list_with_non_digit_fails_closed(self, monkeypatch):
        """A comma-separated port list with a non-digit part fails closed."""
        monkeypatch.setenv("BAD_COMMA_PORT_DSN", "host=h1,h2 port=5432,abcd user=u dbname=d")
        summary = resolve_dsn("BAD_COMMA_PORT_DSN", role="target 'x'").safe_summary()
        assert summary == {"source": "$BAD_COMMA_PORT_DSN", "parsed": "unparseable"}
