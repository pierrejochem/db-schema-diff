"""Lint the SQL files themselves.

These assertions are cheap and catch the two mistakes that would be expensive: a query file
that does not ship in the wheel, and user data interpolated into SQL instead of being passed as
a parameter.
"""

import re
from importlib import resources

import pytest

from db_schema_diff.db.features import ServerFeatures
from db_schema_diff.db.introspect import load_query

QUERY_NAMES = [
    "server",
    "schemas",
    "relations",
    "columns",
    "constraints",
    "indexes",
    "views",
    "sequences",
    "routines",
    "triggers",
    "types",
    "extensions",
    "changelog_locate",
    "changelog_columns",
]

COMMENT = re.compile(r"--[^\n]*")


def sql_body(name):
    """The query with its comments stripped.

    The comments explain *why* a filter is there and name the wrong alternatives, so a lint
    assertion that searched the raw text would match the explanation instead of the SQL.
    """
    return COMMENT.sub("", load_query(name))


def query_files():
    root = resources.files("db_schema_diff.db").joinpath("queries")
    return sorted(p.name for p in root.iterdir() if p.name.endswith(".sql"))


@pytest.mark.parametrize("name", QUERY_NAMES)
def test_every_query_is_loadable_as_package_data(name):
    # Loaded via importlib.resources, so this also proves the files ship in the wheel.
    assert load_query(name).strip()


def test_no_query_file_is_orphaned():
    assert query_files() == sorted(f"{name}.sql" for name in QUERY_NAMES)


@pytest.mark.parametrize("name", QUERY_NAMES)
def test_placeholders_are_all_supplied_by_the_feature_set(name):
    # An unknown {placeholder} would raise KeyError at run time, on a real server, in the
    # middle of a capture. Catch it here instead.
    placeholders = set(re.findall(r"\{(\w+)\}", load_query(name)))
    assert placeholders <= set(ServerFeatures.from_version_num(150004).placeholders())


@pytest.mark.parametrize("name", QUERY_NAMES)
def test_every_query_formats_on_every_supported_version(name):
    for version in (120000, 130000, 140000, 150000, 160000, 170000):
        formatted = load_query(name).format(
            **ServerFeatures.from_version_num(version).placeholders()
        )
        assert "{" not in formatted


@pytest.mark.parametrize("name", QUERY_NAMES)
def test_user_data_travels_as_a_parameter(name):
    sql = load_query(name)
    # Every %(name)s must be a psycopg parameter, i.e. followed by a cast or a comparison,
    # never concatenated into an identifier.
    for parameter in re.findall(r"%\((\w+)\)s", sql):
        assert parameter in {"schemas", "exclude", "only", "schema", "table"}, parameter


@pytest.mark.parametrize("name", QUERY_NAMES)
def test_no_query_contains_a_semicolon(name):
    # One statement per file: a semicolon would allow a second statement to ride along.
    assert ";" not in sql_body(name)


class TestCriticalSafeguards:
    """Each of these is a filter whose absence produces a specific, known flood of noise."""

    def test_the_owned_sequence_lookup_cannot_duplicate_a_column(self):
        """An index also depends on its table's columns with deptype 'a'.

        So the sequence lookup has to be a LATERAL that yields at most one row. A plain join to
        pg_depend with pg_class filtered afterwards emits one row per dependent index, which
        inflates the dense ordinal by a count that varies with how many indexes mention the column.
        """
        sql = sql_body("columns")
        assert "LEFT JOIN LATERAL" in sql
        assert "LIMIT 1" in sql

    def test_constraint_backed_indexes_are_excluded(self):
        # Otherwise every primary key and unique constraint is reported twice.
        sql = sql_body("indexes")
        assert "conindid = i.indexrelid" in sql
        assert "contype IN ('p', 'u', 'x')" in sql

    def test_not_null_constraints_are_never_selected(self):
        # PostgreSQL 17 exposes NOT NULL as contype 'n'; including it would invent one constraint
        # per column when comparing a 15 against a 17.
        sql = sql_body("constraints")
        assert "contype IN ('p', 'u', 'f', 'c', 'x')" in sql
        assert "'n'" not in sql

    def test_constraint_columns_are_resolved_to_names_in_order(self):
        # Attribute numbers differ between databases whose columns were added in a different order.
        sql = sql_body("constraints")
        assert "WITH ORDINALITY" in sql
        assert "a.attname" in sql

    def test_internal_triggers_are_excluded(self):
        # PostgreSQL creates an internal trigger pair per foreign key, so a 200-table schema has
        # thousands of them and every one would be reported.
        assert "NOT t.tgisinternal" in sql_body("triggers")

    def test_a_tables_row_type_is_not_reported_as_a_composite_type(self):
        # Every table creates a composite type of the same name; without this the composite-type
        # section mirrors the table section exactly.
        sql = sql_body("types")
        assert "rel.relkind = 'c'" in sql

    def test_routine_bodies_come_from_prosrc_not_from_pg_get_functiondef(self):
        # pg_get_functiondef re-prints the whole CREATE statement, and its header formatting
        # changes between major releases.
        sql = sql_body("routines")
        assert "p.prosrc" in sql
        assert "pg_get_functiondef" not in sql

    def test_routine_identity_includes_its_arguments(self):
        # Overloads are distinct objects that share a name.
        assert "pg_get_function_identity_arguments" in sql_body("routines")

    def test_sequences_do_not_capture_their_position(self):
        # last_value and is_called are runtime state: a production sequence has issued more values
        # than a QA one by definition.
        sql = sql_body("sequences")
        for runtime_only in ("last_value", "is_called"):
            assert runtime_only not in sql

    def test_views_do_not_capture_whether_a_matview_is_populated(self):
        assert "ispopulated" not in sql_body("views")

    @pytest.mark.parametrize("name", ["views", "sequences", "types"])
    def test_extension_owned_objects_are_excluded_from_every_kind(self, name):
        assert "deptype = 'e'" in sql_body(name)

    @pytest.mark.parametrize("name", ["columns", "constraints", "indexes"])
    def test_leaf_partitions_are_excluded(self, name):
        """A leaf's structure is inherited from its parent and cannot differ.

        Production runs a maintenance job that adds a partition a month; without this filter a
        comparison against a development database reports hundreds of findings for objects nobody
        wrote.
        """
        assert "relispartition" in sql_body(name)

    def test_indexes_are_decomposed_rather_than_string_compared(self):
        sql = sql_body("indexes")
        for column in ("indisunique", "indnkeyatts", "indoption", "indclass", "indpred"):
            assert column in sql

    def test_columns_uses_a_dense_ordinal_not_attnum(self):
        sql = sql_body("columns")
        assert "row_number() OVER" in sql
        assert "NOT a.attisdropped" in sql

    def test_columns_takes_the_type_from_format_type(self):
        sql = sql_body("columns")
        assert "format_type(a.atttypid, a.atttypmod)" in sql
        # The four-column information_schema reconstruction is the classic way to get this
        # wrong, so it must not appear.
        assert "character_maximum_length" not in sql
        assert "numeric_precision" not in sql

    def test_columns_takes_nullability_from_attnotnull(self):
        # Not from pg_constraint: PostgreSQL 17 exposes NOT NULL there, and reading it would
        # invent one constraint per column when comparing a 15 against a 17.
        assert "NOT a.attnotnull" in sql_body("columns")

    def test_columns_finds_the_owned_sequence(self):
        sql = sql_body("columns")
        assert "deptype = 'a'" in sql

    @pytest.mark.parametrize("name", ["relations", "columns", "schemas"])
    def test_extension_owned_objects_are_excluded(self, name):
        # postgis, uuid-ossp and pg_trgm otherwise contribute thousands of findings.
        assert "deptype = 'e'" in sql_body(name)

    def test_schemas_excludes_every_system_schema_with_one_pattern(self):
        sql = sql_body("schemas")
        # pg\_% covers pg_catalog, pg_toast and every numbered pg_temp_N, which cannot be
        # enumerated. The percent is doubled for psycopg; see test_literal_percents_are_escaped.
        assert r"NOT LIKE 'pg\_%%'" in sql
        assert "information_schema" in sql

    def test_changelog_locate_scans_every_schema(self):
        sql = sql_body("changelog_locate")
        # Must not be restricted to %(schemas)s: the changelog's schema may be excluded from
        # the DDL comparison, and its history should still be read.
        assert "%(schemas)s" not in sql
        assert "lower(c.relname)" in sql

    def test_relations_does_not_capture_runtime_state(self):
        sql = sql_body("relations")
        for runtime_only in ("relpages", "reltuples", "last_value"):
            assert runtime_only not in sql


@pytest.mark.parametrize("name", QUERY_NAMES)
def test_literal_percents_are_escaped(name):
    """psycopg parses placeholders in every query, since a mapping is always passed.

    A lone ``%`` that is not part of ``%(name)s`` is then read as a malformed placeholder and the
    query fails at run time, against a real server. This catches it here.
    """
    sql = sql_body(name)
    without_parameters = re.sub(r"%\(\w+\)s", "", sql)
    for match in re.finditer(r"%+", without_parameters):
        assert len(match.group(0)) % 2 == 0, f"unescaped percent in {name}.sql"


class TestGeneratedObjectsAreExcluded:
    """Objects PostgreSQL generates, rather than ones somebody wrote.

    Each was found by reading a real inventory rather than by reasoning about the catalog: one
    range type contributed five constructor functions and one derived multirange type.
    """

    def test_internally_generated_routines_are_excluded(self):
        # CREATE TYPE ... AS RANGE generates a constructor function per signature.
        assert "deptype IN ('e', 'i')" in sql_body("routines")

    def test_derived_multirange_types_are_excluded(self):
        # PostgreSQL 14+ creates one per range type; it cannot differ without its range differing.
        sql = sql_body("types")
        assert "t.typtype IN ('e', 'd', 'c', 'r')" in sql

    def test_array_types_are_excluded(self):
        # An array type is an implementation detail of its element type.
        assert "typcategory <> 'A'" in sql_body("types")
