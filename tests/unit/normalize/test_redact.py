"""Masking a secret must not make two different secrets equal.

A constant '***' would do exactly that, and a drift detector that silently reports no change is
the worst failure this tool has; it has already shipped one (a FAILED changeset passing the gate
at exit 0).
"""

import time

import pytest

from cumo_schema_comparer.normalize.redact import MASK_PREFIX, mask_literals

FUNCTION = """CREATE FUNCTION sync() RETURNS void AS $$
BEGIN
  -- don't change this without asking
  PERFORM dblink_connect('postgresql://u:s3cret@h/db');
  RAISE NOTICE 'ok';
END
$$ LANGUAGE plpgsql"""


class TestMasking:
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
        assert mask_literals("x = 'postgresql://u:aaa@h/db'") == "x = '***:97ede485dfa9'"

    def test_masking_is_idempotent(self):
        once = mask_literals("x = 'postgresql://u:aaa@h/db'")
        assert mask_literals(once) == once

    def test_masking_a_function_body_is_idempotent(self):
        once = mask_literals(FUNCTION)
        assert mask_literals(once) == once

    def test_a_literal_that_already_looks_like_a_mask_is_left_alone(self):
        sql = "WHERE note = '***:a3f1'"
        assert mask_literals(sql) == sql

    def test_a_mask_prefix_does_not_shield_a_secret(self):
        assert "s3cret" not in mask_literals("x = '***:postgresql://u:s3cret@h/db'")

    def test_none_passes_through(self):
        assert mask_literals(None) is None

    def test_a_multiline_body_keeps_its_shape(self):
        body = "BEGIN\n  PERFORM dblink('postgresql://u:s3cret@h/db');\n  RETURN 1;\nEND"
        out = mask_literals(body)
        assert out.splitlines()[0] == "BEGIN"
        assert out.splitlines()[-1] == "END"
        assert "s3cret" not in out


class TestRoutineBodies:
    def test_an_apostrophe_in_a_comment_does_not_hide_a_secret(self):
        out = mask_literals(FUNCTION)
        assert "s3cret" not in out
        assert MASK_PREFIX in out

    def test_everything_but_the_secret_survives_unchanged(self):
        out = mask_literals(FUNCTION)
        for kept in (
            "CREATE FUNCTION sync() RETURNS void AS $$\nBEGIN\n",
            "  -- don't change this without asking\n",
            "  PERFORM dblink_connect('",
            "');\n  RAISE NOTICE 'ok';\nEND\n$$ LANGUAGE plpgsql",
        ):
            assert kept in out

    def test_the_body_is_not_masked_as_a_whole(self):
        out = mask_literals(FUNCTION)
        assert out.count(MASK_PREFIX) == 1
        assert len(out) > len(FUNCTION) / 2

    def test_a_secret_in_a_named_dollar_quote(self):
        sql = "AS $func$ BEGIN PERFORM f('postgresql://u:s3cret@h/db'); END $func$"
        out = mask_literals(sql)
        assert "s3cret" not in out
        assert out.startswith("AS $func$ BEGIN PERFORM f('")
        assert out.endswith("'); END $func$")

    def test_a_dollar_quoted_string_value_is_masked(self):
        out = mask_literals("x = $$postgresql://u:s3cret@h/db$$")
        assert "s3cret" not in out
        assert out.startswith("x = $$") and out.endswith("$$")

    def test_nested_dollar_quotes(self):
        sql = "$a$ EXECUTE $b$ SELECT f('password=s3cret') $b$; $a$"
        assert "s3cret" not in mask_literals(sql)

    def test_an_apostrophe_in_a_comment_before_and_after_a_secret(self):
        sql = "$$ -- it's\nf('password=s3cret');\n-- don't\n g('ok') $$"
        out = mask_literals(sql)
        assert "s3cret" not in out
        assert "-- it's\n" in out and "-- don't\n" in out
        assert "g('ok')" in out

    def test_a_block_comment_apostrophe(self):
        sql = "$$ /* can't */ f('password=s3cret') $$"
        out = mask_literals(sql)
        assert "s3cret" not in out
        assert "/* can't */" in out


class TestEscapes:
    def test_a_backslash_escaped_literal(self):
        out = mask_literals("x = E'it\\'s password=s3cret'")
        assert "s3cret" not in out
        assert out.startswith("x = E'")

    def test_a_backslash_escape_hides_nothing_after_it(self):
        out = mask_literals("x = E'a\\'b' AND y = 'postgresql://u:s3cret@h/d'")
        assert "s3cret" not in out
        assert out.startswith("x = E'a\\'b' AND y = '")

    def test_a_doubled_quote_does_not_end_the_literal(self):
        out = mask_literals("x = 'it''s password=s3cret' AND y = 'ok'")
        assert "s3cret" not in out
        assert out.endswith(" AND y = 'ok'")

    def test_a_doubled_quote_literal_next_to_a_secret(self):
        out = mask_literals("f('it''s', 'postgresql://u:s3cret@h/d')")
        assert out.startswith("f('it''s', '")
        assert "s3cret" not in out

    def test_each_literal_is_judged_on_its_own(self):
        out = mask_literals("f('postgresql://u:s3cret@h/db', 'plain')")
        assert "'plain'" in out
        assert "s3cret" not in out


class TestUnterminated:
    """The tokenizer ends an unterminated literal at the end of the input, so the remainder is
    judged and masked as one literal and left unterminated. Nothing after the quote leaks."""

    def test_an_unterminated_secret_is_masked(self):
        out = mask_literals("x = 'postgresql://u:s3cret@h/db")
        assert "s3cret" not in out

    def test_an_unterminated_literal_swallows_what_follows(self):
        out = mask_literals("x = 'password=s3cret AND y = 2")
        assert "s3cret" not in out
        assert "AND y" not in out

    def test_an_unterminated_ordinary_literal_is_untouched(self):
        assert mask_literals("x = 'abc") == "x = 'abc"

    def test_masking_an_unterminated_literal_is_idempotent(self):
        once = mask_literals("x = 'postgresql://u:s3cret@h/db")
        assert mask_literals(once) == once


@pytest.mark.parametrize(
    "body",
    [
        "SELECT f('postgresql://u:s3cret@h/db'); " * 5_000,
        "x" * 200_000,
        "'" + "x" * 200_000,
        "$$ -- it's\n" + "SELECT 'a'' b'; " * 15_000 + "$$",
        "$a$" * 60_000,
        "''" * 100_000,
    ],
    ids=["many-secrets", "unbroken", "unterminated", "dollar-body", "tags", "doubled"],
)
def test_a_200kb_body_masks_in_well_under_a_second(body):
    start = time.perf_counter()
    mask_literals(body)
    assert time.perf_counter() - start < 1.0


KEYWORDS = ["password", "passfile", "sslpassword", "sslkey"]


class TestPerLiteralDecisions:
    """The decision is made per literal and per comment, never per routine body."""

    def test_a_secret_in_a_comment_is_masked_and_the_rest_is_identical(self):
        out = mask_literals("-- sync from postgresql://u:s3cret@h/db\nSELECT 1")
        assert "s3cret" not in out
        assert out.startswith("-- ***:") and out.endswith("\nSELECT 1")

    def test_a_body_with_no_literals_and_a_column_called_password_is_untouched(self):
        sql = "$$ BEGIN UPDATE t SET password = other_col; END $$"
        assert mask_literals(sql) == sql

    def test_a_password_literal_in_a_body_with_other_literals_is_masked(self):
        sql = "$$ BEGIN x('a'); UPDATE t SET password = 'hunter2' WHERE n = 'b'; END $$"
        out = mask_literals(sql)
        assert "hunter2" not in out
        assert out.startswith("$$ BEGIN x('a'); UPDATE t SET password = '***:")
        assert out.endswith("' WHERE n = 'b'; END $$")

    @pytest.mark.parametrize("keyword", KEYWORDS)
    @pytest.mark.parametrize("spacing", ["{k}='x'", "{k} ='x'", "{k} = 'x'", "{k}= 'x'"])
    def test_the_literal_after_a_secret_keyword_is_masked(self, keyword, spacing):
        out = mask_literals("f(" + spacing.format(k=keyword) + ")")
        assert out.startswith(f"f({keyword}")  # the keyword itself stays readable
        assert "'x'" not in out
        assert MASK_PREFIX in out

    def test_the_keyword_match_is_case_insensitive(self):
        assert "'x'" not in mask_literals("PassWord = 'x'")

    def test_an_escape_string_after_a_keyword_is_masked(self):
        out = mask_literals("password = E'a\\'b'")
        assert "a\\'b" not in out and out.startswith("password = E'")

    def test_a_dollar_quoted_value_after_a_keyword_is_masked(self):
        out = mask_literals("password = $$hunter2$$")
        assert "hunter2" not in out and out.startswith("password = $$")

    def test_a_literal_after_another_operator_is_left_alone(self):
        sql = "WHERE password <> 'x' AND note = 'y'"
        assert mask_literals(sql) == sql

    def test_a_different_password_masks_differently(self):
        assert mask_literals("password = 'a'") != mask_literals("password = 'b'")

    def test_a_dollar_quoted_url_value_is_masked_without_a_literal_inside(self):
        assert "s3cret" not in mask_literals("SELECT $q$postgresql://u:s3cret@h/db$q$")


class TestComments:
    @pytest.mark.parametrize(
        ("sql", "kept"),
        [
            ("-- see postgresql://u:s3cret@h/db\nSELECT 1", "\nSELECT 1"),
            ("/* see postgresql://u:s3cret@h/db */ SELECT 1", " */ SELECT 1"),
            ("SELECT 1 -- password=s3cret", ""),
            ("/* password=s3cret", ""),
            (
                "$$ BEGIN -- see postgresql://u:s3cret@h/db\n x('a'); END $$",
                "\n x('a'); END $$",
            ),
            ("$$ BEGIN /* password=s3cret */ x('a'); END $$", " */ x('a'); END $$"),
        ],
    )
    def test_a_credential_in_a_comment_is_masked(self, sql, kept):
        out = mask_literals(sql)
        assert "s3cret" not in out
        assert MASK_PREFIX in out
        assert out.endswith(kept)

    def test_the_comment_delimiters_survive(self):
        assert mask_literals("/* password=s3cret */").startswith("/* ***:")
        assert mask_literals("/* password=s3cret */").endswith(" */")

    def test_a_comment_that_merely_mentions_the_word_is_untouched(self):
        for sql in (
            "-- the password is rotated nightly\nSELECT 1",
            "/* never log the password, see ticket */ SELECT 1",
            "$$ BEGIN -- password handling below\n x(1); END $$",
        ):
            assert mask_literals(sql) == sql

    def test_a_changed_secret_in_a_comment_still_reads_as_changed(self):
        one = mask_literals("-- password=aaa")
        two = mask_literals("-- password=bbb")
        assert one != two

    def test_masking_comments_is_idempotent(self):
        once = mask_literals("-- password=s3cret\n/* postgresql://u:p@h/d */")
        assert mask_literals(once) == once


def test_idempotence_and_distinctness_across_all_forms():
    sql = "$$ -- password=aaa\n x('postgresql://u:aaa@h/d'); password = 'aaa' $$"
    once = mask_literals(sql)
    assert mask_literals(once) == once
    assert mask_literals(sql.replace("aaa", "bbb")) != once


def test_a_200kb_body_with_every_kind_of_secret_is_fast():
    unit = "-- password=s3cret\nx('postgresql://u:s@h/d'); password = 'p'; /* it's */\n"
    body = "$$ " + unit * (200_000 // len(unit)) + "$$"
    start = time.perf_counter()
    mask_literals(body)
    assert time.perf_counter() - start < 1.0
