"""What the normaliser produces from real PostgreSQL catalog output.

The no-drift test proves master and target agree, but it applies the same DDL to both, so a
normaliser that did nothing at all would also pass it. These assertions pin the actual canonical
values, which is the only way to know the normalisation is *right* rather than merely consistent.

Every expected value below was checked against PostgreSQL 15's own output, not assumed.
"""

from __future__ import annotations

import pytest

from cumo_schema_comparer.model.objects import SERIAL_SENTINEL
from tests.integration.test_no_drift import inventory_of

pytestmark = pytest.mark.integration

TABLE = "cumo-invoicing.column_flavours"


@pytest.fixture(scope="module")
def flavours(read_only_master):
    """Columns of the kitchen-sink table, keyed by column name.

    Captured once: every assertion here only reads it.
    """
    inventory = inventory_of(read_only_master, "prod")
    return {
        key.subname: obj
        for key, obj in inventory.objects.items()
        if key.qualified == TABLE and key.subname
    }


class TestTypes:
    @pytest.mark.parametrize(
        ("column", "expected"),
        [
            # Long spellings collapse to the short canonical one.
            ("code_varchar", "varchar(12)"),
            ("code_bpchar", "bpchar(3)"),
            ("code_text", "text"),
            ("ts_plain", "timestamp"),
            ("ts_tz", "timestamptz"),
            ("t_plain", "time"),
            ("t_tz", "timetz"),
            ("small_serial", "int2"),
            ("big_serial", "int8"),
            # A precision modifier is part of the type and must survive the aliasing.
            ("ts_precise", "timestamp(3)"),
            ("ts_tz_precise", "timestamptz(3)"),
            # An unconstrained numeric is NOT a constrained one. Collapsing these would hide the
            # most common real column change there is.
            ("amount_free", "numeric"),
            ("amount_scaled", "numeric(12,2)"),
            # An explicit zero scale means what no scale means.
            ("amount_zero", "numeric(10)"),
            ("amount_int", "numeric(10)"),
            # Arrays: PostgreSQL does not enforce declared dimensions.
            ("tags", "text[]"),
            ("tag_matrix", "int4[]"),
            ("scores", "numeric(5,2)[]"),
            # Types with their own printing rules.
            ("payload", "jsonb"),
            ("legacy_payload", "json"),
            ("external_id", "uuid"),
            ("raw_bytes", "bytea"),
            ("client_ip", "inet"),
            ("flags", "varbit(8)"),
            ("fixed_flags", "bit(4)"),
            ("money_amount", "money"),
            ("span", "interval"),
            ("span_precise", "interval hour to second(3)"),
        ],
    )
    def test_the_canonical_type_is_the_short_form(self, flavours, column, expected):
        assert flavours[column].data_type == expected

    def test_a_user_type_keeps_its_qualifier_and_its_quoting(self, flavours):
        # With search_path='' the server qualifies user types, and the hyphen forces quoting.
        assert flavours["stage"].data_type == '"cumo-invoicing".dunning_stage'
        assert flavours["vetted_amount"].data_type == '"cumo-invoicing".positive_amount'

    def test_the_servers_own_spelling_is_kept_for_display(self, flavours):
        assert flavours["ts_plain"].raw.get("data_type") == "timestamp without time zone"
        assert flavours["code_varchar"].raw.get("data_type") == "character varying(12)"


class TestDefaults:
    @pytest.mark.parametrize(
        ("column", "expected"),
        [
            ("d_literal", "42"),
            ("d_bool", "false"),
            ("d_expression", "2 * 21"),
            ("d_func", "gen_random_uuid()"),
            ("required", "0"),
            # Redundant presentation the server adds back, removed.
            ("d_paren", "0"),
            ("d_string", "'DRAFT'"),
            ("d_quoted", "'it''s here'"),
            ("d_empty_array", "'{}'"),
            ("d_json", "'{}'"),
            # Keyword case folds; equivalent spellings converge.
            ("d_now", "now()"),
            ("d_current", "now()"),
            ("d_user", "current_user"),
        ],
    )
    def test_the_canonical_default(self, flavours, column, expected):
        assert flavours[column].default == expected

    def test_two_spellings_of_the_same_default_converge(self, flavours):
        # DEFAULT now() and DEFAULT CURRENT_TIMESTAMP are stored differently by the server, so
        # without this they would read as drift between two identical environments forever.
        assert flavours["d_now"].raw.get("default") != flavours["d_current"].raw.get("default")
        assert flavours["d_now"].default == flavours["d_current"].default

    def test_a_qualified_function_default_keeps_its_qualifier(self, flavours):
        assert flavours["d_qualified"].default == '"cumo-invoicing".next_reference()'

    def test_a_cast_to_a_user_type_is_kept(self, flavours):
        # The cast names the enum, which is information, not noise.
        assert flavours["d_enum"].default == "'NONE'::\"cumo-invoicing\".dunning_stage"

    def test_an_array_constructor_default_binds_to_its_bracket(self, flavours):
        assert flavours["d_array_items"].default == "array['a'::text, 'b'::text]"


class TestAutoincrement:
    def test_a_serial_default_becomes_the_sentinel(self, flavours):
        # The generated sequence name diverges between environments; the strategy is what counts.
        for column in ("small_serial", "big_serial"):
            assert flavours[column].default == SERIAL_SENTINEL
            assert flavours[column].autoincrement == "serial"
            assert flavours[column].sequence_name is not None

    def test_the_two_identity_kinds_are_distinguished(self, flavours):
        # ALWAYS rejects a user-supplied value and BY DEFAULT accepts one. Different behaviour.
        assert flavours["id_always"].autoincrement == "identity:a"
        assert flavours["id_by_default"].autoincrement == "identity:d"

    def test_identity_and_serial_are_not_the_same_strategy(self, flavours):
        assert flavours["id_by_default"].autoincrement != flavours["big_serial"].autoincrement


class TestGeneratedColumns:
    def test_a_stored_generated_column_is_not_recorded_as_a_default(self, flavours):
        computed = flavours["computed"]
        assert computed.default is None
        assert computed.generated is not None

    def test_a_cast_on_one_operand_of_the_expression_survives(self, flavours):
        """The server prints ``(amount_scaled * 2::numeric)``.

        Dropping that cast would make numeric and integer division canonicalise identically, and
        the tool would report two genuinely different generated columns as in sync.
        """
        assert "::numeric" in (flavours["computed"].generated or "")


class TestCollation:
    def test_a_non_default_collation_is_captured(self, flavours):
        assert flavours["sorted_name"].collation == "C"

    def test_a_column_without_an_explicit_collation_records_none(self, flavours):
        # Otherwise every text column would carry the database collation and drift whenever two
        # environments were initialised differently.
        assert flavours["code_text"].collation is None


class TestNullability:
    def test_not_null_is_read_from_the_attribute_not_from_a_constraint(self, flavours):
        # PostgreSQL 17 also exposes NOT NULL as a pg_constraint row; reading it there would
        # invent one constraint per column when comparing a 15 against a 17.
        assert flavours["required"].is_nullable is False
        assert flavours["code_text"].is_nullable is True

    def test_a_column_can_be_not_null_and_have_a_default(self, flavours):
        assert flavours["required"].is_nullable is False
        assert flavours["required"].default == "0"
