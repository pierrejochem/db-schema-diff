"""The credential-shape detector, now in the library so build-time code can use it.

These are the shapes that leaked during the GUI work. Each one is here because it reached a
rendered string in a real run, not because it seemed plausible.
"""

import time

import pytest

from cumo_schema_comparer.literals import looks_like_connection_string

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
