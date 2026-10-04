"""Expression canonicalization and schema remapping."""

import pytest

from db_schema_comparer.normalize.expressions import (
    canonical_expr,
    exprs_equivalent,
    remap_schema_qualifiers,
)


class TestWhitespaceAndParens:
    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("a  =   b", "a = b"),
            ("a\n=\tb", "a = b"),
            ("lower( email )", "lower(email)"),
            ("(a = b)", "a = b"),
            ("(((a = b)))", "a = b"),
            ("((0))", "0"),
            ("(-1)", "-1"),
            ("f(a, b)", "f(a,b)"),
        ],
    )
    def test_cosmetic_differences_collapse(self, left, right):
        assert canonical_expr(left) == canonical_expr(right)

    def test_parens_that_carry_meaning_are_kept(self):
        assert canonical_expr("(a + b) * c") != canonical_expr("a + b * c")

    def test_unbalanced_parens_are_left_alone_rather_than_stripped(self):
        # A malformed expression must not be silently mangled into a different one.
        assert canonical_expr("(a = b") == "(a = b"

    def test_whitespace_inside_a_literal_is_preserved(self):
        assert canonical_expr("'two  spaces'") == "'two  spaces'"

    def test_a_trailing_semicolon_is_dropped(self):
        assert canonical_expr("SELECT 1;") == canonical_expr("SELECT 1")


class TestRedundantCasts:
    def test_a_self_cast_on_a_matching_column_type_is_dropped(self):
        assert canonical_expr("'foo'::character varying", column_type="varchar(50)") == "'foo'"
        assert canonical_expr("'foo'::varchar", column_type="varchar") == "'foo'"

    def test_a_cast_to_a_different_type_is_real_and_kept(self):
        # 'foo'::text on a varchar column is a genuine difference, not noise.
        assert "::text" in canonical_expr("'foo'::text", column_type="varchar(50)")

    def test_a_numeric_self_cast_is_dropped(self):
        assert canonical_expr("(0)::numeric", column_type="numeric(10,2)") == "0"

    def test_without_a_column_type_no_cast_is_assumed_redundant(self):
        assert canonical_expr("'foo'::character varying") == "'foo'::varchar"

    def test_cast_type_spelling_is_canonicalized(self):
        assert exprs_equivalent("x::character varying(10)", "x::varchar(10)")
        assert exprs_equivalent("x::integer", "x::int4")


class TestKnownEquivalences:
    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("now()", "CURRENT_TIMESTAMP"),
            ("now()", "('now'::text)::timestamp with time zone"),
            ("CURRENT_USER", '"current_user"()'),
            ("CURRENT_USER", "current_user"),
        ],
    )
    def test_spellings_of_the_same_value_are_equivalent(self, left, right):
        assert exprs_equivalent(left, right)

    def test_unrelated_functions_are_not_equivalent(self):
        assert not exprs_equivalent("now()", "statement_timestamp()")
        assert not exprs_equivalent("now()", "CURRENT_DATE")


class TestKeywordCase:
    def test_keywords_fold_but_quoted_identifiers_do_not(self):
        assert exprs_equivalent("a = ANY (ARRAY['A'])", "a = any (array['A'])")
        assert not exprs_equivalent('"MyCol" = 1', '"mycol" = 1')

    def test_literal_case_is_significant(self):
        assert not exprs_equivalent("x = 'A'", "x = 'a'")


class TestSchemaRemapping:
    def test_a_bare_schema_qualifier_is_rewritten(self):
        assert remap_schema_qualifiers(
            "invoicing_qa.gen_id()", {"invoicing_qa": "acme-invoicing"}
        ) == ('"acme-invoicing".gen_id()')

    def test_a_quoted_schema_qualifier_is_rewritten(self):
        result = remap_schema_qualifiers(
            '"invoicing_qa".gen_id()', {"invoicing_qa": "acme-invoicing"}
        )
        assert result == '"acme-invoicing".gen_id()'

    def test_a_name_needing_no_quotes_is_left_unquoted(self):
        assert (
            remap_schema_qualifiers("qa_schema.f()", {"qa_schema": "prod_schema"})
            == "prod_schema.f()"
        )

    def test_only_schema_positions_are_rewritten(self):
        # A column or function that happens to share the schema's name must not be touched.
        assert remap_schema_qualifiers("qa.qa", {"qa": "prod"}) == "prod.qa"

    def test_a_matching_string_literal_is_not_rewritten(self):
        assert remap_schema_qualifiers("'qa.thing'", {"qa": "prod"}) == "'qa.thing'"

    def test_a_schema_inside_a_regclass_literal_is_rewritten(self):
        # nextval('qa.t_id_seq'::regclass) genuinely names a schema inside a literal.
        result = remap_schema_qualifiers("nextval('qa.t_id_seq'::regclass)", {"qa": "prod"})
        assert result == "nextval('prod.t_id_seq'::regclass)"

    def test_an_empty_mapping_returns_the_input_unchanged(self):
        assert remap_schema_qualifiers("qa.f()", {}) == "qa.f()"


def test_none_passes_through():
    assert canonical_expr(None) is None
    assert remap_schema_qualifiers(None, {"a": "b"}) is None


class TestCastRedundancyBoundary:
    """A cast is redundant only when it cannot change the value."""

    def test_an_unmodified_cast_to_the_same_base_type_is_redundant(self):
        assert canonical_expr("'x'::varchar", column_type="varchar(50)") == "'x'"

    def test_a_cast_with_the_columns_own_modifier_is_redundant(self):
        assert canonical_expr("'x'::varchar(50)", column_type="varchar(50)") == "'x'"

    def test_a_narrower_cast_truncates_and_is_kept(self):
        # x::varchar(10) on a varchar(50) column changes the value. Never drop this.
        assert "::varchar(10)" in canonical_expr("'x'::varchar(10)", column_type="varchar(50)")

    def test_a_wider_cast_is_kept_too(self):
        assert "::varchar(80)" in canonical_expr("'x'::varchar(80)", column_type="varchar(50)")

    def test_a_different_base_type_is_kept(self):
        assert "::text" in canonical_expr("'x'::text", column_type="varchar(50)")

    def test_an_array_cast_matches_an_array_column(self):
        assert canonical_expr("'{}'::text[]", column_type="text[]") == "'{}'"
        assert "::text" in canonical_expr("'x'::text", column_type="text[]")


class TestCastStrippingIsTopLevelOnly:
    """A cast is only redundant when it applies to the *whole* expression.

    ``'DRAFT'::character varying`` on a varchar column coerces the entire default and says
    nothing. ``a / 2::numeric`` casts one operand, and dropping it turns numeric division into
    integer division — a real semantic change that would then be hidden from the report.
    """

    def test_a_cast_over_the_whole_expression_is_stripped(self):
        assert canonical_expr("'DRAFT'::character varying", column_type="varchar(16)") == "'DRAFT'"
        assert canonical_expr("0::numeric", column_type="numeric(8,2)") == "0"
        assert canonical_expr("(0)::numeric", column_type="numeric(8,2)") == "0"
        assert canonical_expr("'{}'::text[]", column_type="text[]") == "'{}'"

    def test_a_cast_on_one_operand_is_kept(self):
        # Observed in a real GENERATED column: the server prints (amount * 2::numeric).
        result = canonical_expr("amount_scaled * 2::numeric", column_type="numeric(14,4)")
        assert "::numeric" in result

    def test_integer_versus_numeric_division_stays_distinguishable(self):
        # The case that makes this matter: these two compute different values.
        integer_division = canonical_expr("a / 2", column_type="numeric(14,4)")
        numeric_division = canonical_expr("a / 2::numeric", column_type="numeric(14,4)")
        assert integer_division != numeric_division

    def test_a_cast_inside_a_function_call_is_kept(self):
        result = canonical_expr("coalesce(a, 0::numeric)", column_type="numeric(8,2)")
        assert "::numeric" in result

    def test_a_cast_on_a_parenthesized_sub_expression_is_kept(self):
        result = canonical_expr("(a + b)::numeric * c", column_type="numeric(8,2)")
        assert "::numeric" in result

    def test_a_whole_expression_cast_wrapping_a_sub_cast_strips_only_the_outer_one(self):
        result = canonical_expr("(a * 2::numeric)::numeric", column_type="numeric(8,2)")
        assert result.count("::numeric") == 1


class TestArraySpacing:
    """``ARRAY[...]`` and a subscript both bind tightly to their bracket."""

    def test_an_array_constructor_has_no_space_before_its_bracket(self):
        assert canonical_expr("ARRAY['a'::text]") == "array['a'::text]"

    def test_a_subscript_has_no_space_before_its_bracket(self):
        assert canonical_expr("tags[1]") == "tags[1]"

    def test_both_spellings_of_an_array_constructor_converge(self):
        assert exprs_equivalent("ARRAY['a'::text, 'b'::text]", "array ['a'::text,'b'::text]")
