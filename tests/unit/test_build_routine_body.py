"""The builder keeps the canonical body it already computes for the hash."""

from cumo_schema_comparer.build import _routines
from cumo_schema_comparer.normalize.routines import body_hash, canonical_body

SRC = "BEGIN\n  RETURN a  +  b;\nEND"


def row(**extra):
    return {
        "schema": "public",
        "name": "f_add",
        "identity_arguments": "a integer, b integer",
        "language": "plpgsql",
        **extra,
    }


def only(rows):
    return next(iter(_routines(rows).values()))


def test_the_builder_keeps_the_canonical_body():
    r = only([row(body=SRC)])
    assert r.body == canonical_body(SRC)
    assert r.body is not None
    assert r.body_hash == body_hash(SRC)


def test_a_standard_body_function_keeps_its_sql_body():
    src = "RETURN (a + b)"
    r = only([row(body="", sql_body=src, language="sql")])
    assert r.body == canonical_body(src)
    assert r.body is not None
    assert r.body_hash == body_hash(src)
