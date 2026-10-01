"""The credential-shape detector, now in the library so build-time code can use it.

These are the shapes that leaked during the GUI work. Each one is here because it reached a
rendered string in a real run, not because it seemed plausible.
"""

import time

import pytest

from cumo_schema_comparer.literals import (
    MASK_PREFIX,
    looks_like_connection_string,
    mask_literals,
)

REJECTED = [
    "postgresql://u:s3cret@h/db",
    "postgres://x",
    "POSTGRESQL://h",
    "https://u:p@example.com/x",
    "host=h password=s3cret",
    "password='Zq7x PLUMBUS9'",
    'password="Zq7x PLUMBUS9"',
    "password=Zq7x\\ PLUMBUS9",
    "password='P@ssw0rd hunter2'",
    "sslpassword=p://x hunter2",
    "passfile=/x/p@ss word",
    "sslkey=/tmp/k",
    "password='unterminated hunter2",
]

ACCEPTED = [
    "see https://jira.example.com/X-1",
    "ops set user = readonly here",
    "service=batch creates it",
    "http://docs.example.com",
    "my-password=notakeyword",
    "quartz tables",
    "cumo-invoicing",
]


@pytest.mark.parametrize("value", REJECTED)
def test_a_credential_shape_is_detected(value):
    assert looks_like_connection_string(value) is True


@pytest.mark.parametrize("value", ACCEPTED)
def test_ordinary_prose_is_not(value):
    assert looks_like_connection_string(value) is False


@pytest.mark.parametrize(
    "value",
    [
        "a@b " * 50_000,
        "x" * 200_000,
        "http://" + "a" * 200_000,
        "x" * 200_000 + "://" + "y" * 200_000,
        "a://" * 50_000,
    ],
    ids=["spaced", "unbroken-run", "scheme-then-run", "run-scheme-run", "repeated-scheme"],
)
def test_the_scan_is_linear_not_quadratic(value):
    """A 200 KB value froze the GUI event loop for 103 s before this was fixed.

    The spaced input alone never reached the scheme regex, so the unbroken runs are the real guard:
    a long base64 or hex literal has no spaces and no ``://``.
    """
    start = time.perf_counter()
    looks_like_connection_string(value)
    assert time.perf_counter() - start < 1.0


class TestMasking:
    """Masking a secret must not make two different secrets equal.

    A constant '***' would do exactly that, and a drift detector that silently reports no change is
    the worst failure this tool has; it has already shipped one (a FAILED changeset passing the
    gate at exit 0).
    """

    def test_a_connection_string_literal_is_masked(self):
        out = mask_literals("SET x = 'postgresql://u:s3cret@h/db'")
        assert "s3cret" not in out
        assert MASK_PREFIX in out

    def test_the_surrounding_sql_survives(self):
        out = mask_literals("SELECT dblink_connect('postgresql://u:s3cret@h/db') FROM t")
        assert out.startswith("SELECT dblink_connect(")
        assert out.endswith(") FROM t")

    def test_an_ordinary_literal_is_untouched(self):
        sql = "WHERE status = 'paid' AND n = 42"
        assert mask_literals(sql) == sql

    def test_two_different_secrets_get_different_masks(self):
        one = mask_literals("x = 'postgresql://u:aaa@h/db'")
        two = mask_literals("x = 'postgresql://u:bbb@h/db'")
        assert one != two

    def test_the_same_secret_gets_the_same_mask(self):
        sql = "x = 'postgresql://u:aaa@h/db'"
        assert mask_literals(sql) == mask_literals(sql)

    def test_the_mask_is_a_pinned_unsalted_digest(self):
        # Pinned so a change to the hashing (or a salt) is a deliberate edit to this test.
        assert mask_literals("x = 'postgresql://u:aaa@h/db'") == "x = '***:97ede485'"

    def test_masking_is_idempotent(self):
        once = mask_literals("x = 'postgresql://u:aaa@h/db'")
        assert mask_literals(once) == once

    def test_a_literal_that_already_looks_like_a_mask_is_left_alone(self):
        sql = "WHERE note = '***:a3f1'"
        assert mask_literals(sql) == sql

    def test_a_mask_prefix_does_not_shield_a_secret(self):
        out = mask_literals("x = '***:postgresql://u:s3cret@h/db'")
        assert "s3cret" not in out

    def test_a_doubled_quote_does_not_end_the_literal(self):
        out = mask_literals("x = 'it''s password=s3cret' AND y = 'ok'")
        assert "s3cret" not in out
        assert out.endswith(" AND y = 'ok'")

    def test_each_literal_is_judged_on_its_own(self):
        out = mask_literals("f('postgresql://u:s3cret@h/db', 'plain')")
        assert "'plain'" in out
        assert "s3cret" not in out

    def test_none_passes_through(self):
        assert mask_literals(None) is None

    def test_a_multiline_body_keeps_its_shape(self):
        body = "BEGIN\n  PERFORM dblink('postgresql://u:s3cret@h/db');\n  RETURN 1;\nEND"
        out = mask_literals(body)
        assert out.splitlines()[0] == "BEGIN"
        assert out.splitlines()[-1] == "END"
        assert "s3cret" not in out

    @pytest.mark.parametrize(
        "value",
        ["'" * 100_001, "'a" * 100_000, "'" + "x" * 200_000, "''" * 100_000],
        ids=["quotes", "open-quotes", "unterminated", "doubled"],
    )
    def test_masking_is_linear(self, value):
        start = time.perf_counter()
        mask_literals(value)
        assert time.perf_counter() - start < 1.0
