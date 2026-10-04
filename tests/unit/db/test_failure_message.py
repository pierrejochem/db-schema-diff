"""Connection-failure text must carry no password fragment, even one libpq echoes back."""

from __future__ import annotations

from db_schema_diff.config.secrets import Dsn
from db_schema_diff.db.connect import _failure_message

REASON = "failed to resolve host 'ss@nohost.invalid': [Errno 8] nodename nor servname provided"


def test_mis_parsed_host_fragment_is_scrubbed_from_the_libpq_reason():
    dsn = Dsn("postgresql://u:p@ss@nohost.invalid:5432/db", env_name="X")
    message = _failure_message("target", dsn, RuntimeError(REASON))
    assert "ss@" not in message
    assert "nohost" not in message.split("\n", 1)[1]
    assert "cannot connect" in message


def test_unknown_at_token_is_redacted_by_the_second_layer():
    dsn = Dsn("host=h user=u", env_name="X")
    message = _failure_message("target", dsn, RuntimeError("bad thing zz@qq happened"))
    assert "zz@qq" not in message
    assert "bad thing" in message


def test_keyword_password_is_scrubbed_from_the_reason():
    dsn = Dsn("host=h user=u password=hunter2", env_name="X")
    message = _failure_message("t", dsn, RuntimeError("auth failed with hunter2"))
    assert "hunter2" not in message


def test_slash_password_leaves_no_fragment_anywhere_in_the_message():
    dsn = Dsn("postgresql://u:pa/ss@nohost.invalid:5432/db", env_name="X")
    reason = RuntimeError("failed to resolve host 'u': port 'pa' ss@nohost.invalid:5432/db")
    message = _failure_message("target", dsn, reason)
    assert "pa/ss" not in message
    assert "port=pa" not in message
    assert "ss@" not in message
