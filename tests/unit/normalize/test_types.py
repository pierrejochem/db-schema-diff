"""Type canonicalization.

Every case here is a way two servers can spell the same type, or a way two genuinely
different types can look similar. The distinction between the two is the whole point.
"""

import pytest

from db_schema_comparer.normalize.types import canonical_type, types_equivalent

EQUIVALENT = [
    # Spelling only. The server prints the long form; nobody writes it.
    ("character varying(50)", "varchar(50)"),
    ("character varying", "varchar"),
    ("character(10)", "bpchar(10)"),
    ("timestamp without time zone", "timestamp"),
    ("timestamp with time zone", "timestamptz"),
    ("timestamp(3) without time zone", "timestamp(3)"),
    ("time without time zone", "time"),
    ("time with time zone", "timetz"),
    ("double precision", "float8"),
    ("real", "float4"),
    ("integer", "int4"),
    ("int", "int4"),
    ("smallint", "int2"),
    ("bigint", "int8"),
    ("boolean", "bool"),
    ("decimal(10,2)", "numeric(10,2)"),
    ("bit varying(8)", "varbit(8)"),
    # Whitespace and case from hand-written DDL.
    ("NUMERIC( 10 , 2 )", "numeric(10,2)"),
    ("  integer  ", "int4"),
    # An explicit zero scale is what "no scale" already means.
    ("numeric(10,0)", "numeric(10)"),
    # PostgreSQL does not enforce declared array dimensions, so they carry no meaning.
    ("integer[][]", "int4[]"),
    ("character varying(20)[]", "varchar(20)[]"),
]

DISTINCT = [
    # The single most dangerous false equivalence: an unconstrained numeric is not a
    # constrained one, and collapsing them hides a real schema difference.
    ("numeric", "numeric(10,2)"),
    ("varchar", "varchar(255)"),
    ("varchar(50)", "varchar(60)"),
    ("numeric(10,2)", "numeric(12,2)"),
    ("numeric(10,2)", "numeric(10,3)"),
    ("timestamp(3)", "timestamp(6)"),
    ("int4", "int8"),
    ("text", "varchar(255)"),
    ("int4", "int4[]"),
]


@pytest.mark.parametrize(("left", "right"), EQUIVALENT)
def test_equivalent_spellings_canonicalize_to_one_form(left, right):
    assert canonical_type(left) == canonical_type(right)
    assert types_equivalent(left, right)


@pytest.mark.parametrize(("left", "right"), DISTINCT)
def test_genuinely_different_types_stay_different(left, right):
    assert canonical_type(left) != canonical_type(right)
    assert not types_equivalent(left, right)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("character varying(50)", "varchar(50)"),
        ("numeric", "numeric"),
        ("integer", "int4"),
        ("text[]", "text[]"),
        ("public.status_enum", "public.status_enum"),
        ('"cumo-invoicing".status_enum', '"cumo-invoicing".status_enum'),
    ],
)
def test_canonical_form_is_the_short_one(raw, expected):
    assert canonical_type(raw) == expected


def test_user_type_names_are_never_case_folded():
    # A quoted identifier is case-sensitive, and this platform really does use
    # "cumo-invoicing" as a schema name.
    assert canonical_type('"cumo-invoicing".MyType') == '"cumo-invoicing".MyType'


def test_serial_is_not_a_type():
    # serial never appears in the catalog; it is integer plus a sequence default. A comparer
    # that looks for it is reading something other than pg_catalog.
    assert canonical_type("serial") == "serial"
    assert not types_equivalent("serial", "int4")


def test_none_and_empty_pass_through():
    assert canonical_type(None) is None
    assert canonical_type("") == ""
