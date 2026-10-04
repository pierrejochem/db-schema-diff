"""Masking a secret must not make two different secrets equal.

A constant '***' would do exactly that, and a drift detector that silently reports no change is
the worst failure this tool has; it has already shipped one (a FAILED changeset passing the gate
at exit 0).
"""

import time

import pytest

from db_schema_diff.normalize.redact import MASK_PREFIX, mask_literals

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

    def test_an_unterminated_literal_stays_unterminated_byte_for_byte(self):
        # Pinned exactly: treating it as terminated drops the last byte of the secret from the
        # digest and invents a closing quote, changing both the SQL shape and the mask.
        assert mask_literals("x = 'postgresql://u:s3cret@h/db") == "x = '***:2e8037391e21"

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
        # The conninfo pair scan: all pairs and a secret keyword, so it runs to the last token.
        "$$" + "host=db1 user=u password=s3cret dbname=d " * 4_800 + "$$",
        # All pairs, no secret keyword: the cheap predicate must keep the pair scan off this.
        "$$" + "host=db1 user=u dbname=d " * 8_000 + "$$",
        # The comment token scan, over tokens that each hold a ``://`` and an ``@``.
        "-- " + "git clone git://git@github.com/org/repo password = NULL " * 3_500,
        # One comment token of nothing but colons: the userinfo scan must not backtrack over it.
        "-- a://" + ":" * 200_000,
    ],
    ids=[
        "many-secrets",
        "unbroken",
        "unterminated",
        "dollar-body",
        "tags",
        "doubled",
        "conninfo-pairs",
        "pairs-no-secret",
        "comment-tokens",
        "colons",
    ],
)
def test_a_200kb_body_masks_in_well_under_a_second(body):
    start = time.perf_counter()
    mask_literals(body)
    assert time.perf_counter() - start < 1.0


KEYWORDS = ["password", "passfile", "sslpassword", "sslkey"]


class TestPerLiteralDecisions:
    """The decision is made per literal and per comment, never per routine body."""

    def test_a_secret_in_a_comment_is_masked_and_the_rest_is_identical(self):
        # Only the credential span goes: the prose around it is what a reviewer reads the comment
        # for, and a comment cannot change what the SQL means, so masking inside it is safe.
        out = mask_literals("-- sync from postgresql://u:s3cret@h/db\nSELECT 1")
        assert "s3cret" not in out
        assert out == "-- sync from ***:2e8037391e21\nSELECT 1"

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


class TestAssignmentOperators:
    @pytest.mark.parametrize("keyword", KEYWORDS)
    @pytest.mark.parametrize("operator", ["=", ":=", "=>"])
    @pytest.mark.parametrize("spacing", ["{k}{o}'x'", "{k} {o}'x'", "{k} {o} 'x'"])
    def test_the_literal_after_a_secret_keyword_and_operator_is_masked(
        self, keyword, operator, spacing
    ):
        sql = "f(" + spacing.format(k=keyword, o=operator) + ")"
        out = mask_literals(sql)
        assert "'x'" not in out
        assert out.startswith(f"f({keyword}")
        assert MASK_PREFIX in out

    @pytest.mark.parametrize("operator", ["<=", ">=", "<>", "!=", "<", ">"])
    def test_comparisons_other_than_equals_are_left_alone(self, operator):
        sql = f"WHERE password {operator} 'x'"
        assert mask_literals(sql) == sql

    @pytest.mark.parametrize("sql", ["v_password := 'x'", "other := 'x'", "x = y => 'x'"])
    def test_other_names_and_arrows_are_left_alone(self, sql):
        assert mask_literals(sql) == sql

    def test_plpgsql_assignment_in_a_body(self):
        out = mask_literals("$$ BEGIN password := 'hunter2'; n := 'ok'; END $$")
        assert "hunter2" not in out
        assert "n := 'ok'" in out

    def test_a_named_argument(self):
        out = mask_literals("SELECT connect(host => 'h', password => 'hunter2')")
        assert "hunter2" not in out
        assert "host => 'h'" in out

    def test_assignment_masking_is_idempotent_and_distinct(self):
        once = mask_literals("password := 'a'")
        assert mask_literals(once) == once
        assert once != mask_literals("password := 'b'")


class TestDollarQuotedConninfo:
    """A libpq conninfo in a dollar-quoted value is the shape ``dblink_connect`` and
    ``postgres_fdw`` actually use, and it must not survive into a published report. The
    discriminator against a routine body is structural: a conninfo is nothing but ``key=value``
    pairs, code has bare tokens."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT dblink_connect($$host=db1 user=u password=s3cret dbname=d$$)",
            "SELECT dblink_connect($conn$host=db1 password=s3cret$conn$)",
            "SELECT dblink_connect($$host = db1 password = s3cret$$)",
            "OPTIONS (conninfo $$host=db1 sslkey=/k/p password=s3cret$$)",
        ],
        ids=["anonymous-tag", "named-tag", "spaced-equals", "fdw-options"],
    )
    def test_a_conninfo_in_a_dollar_quoted_value_is_masked(self, sql):
        out = mask_literals(sql)
        assert "s3cret" not in out
        assert MASK_PREFIX in out

    def test_the_quote_tag_and_the_surrounding_sql_survive(self):
        out = mask_literals("SELECT dblink_connect($conn$host=db1 password=s3cret$conn$) FROM t")
        assert out.startswith("SELECT dblink_connect($conn$")
        assert out.endswith("$conn$) FROM t")

    def test_the_dollar_and_single_quoted_forms_mask_identically(self):
        # Rewriting a conninfo from '...' to $$...$$ changes no credential, so it must not read
        # as drift.
        conninfo = "host=db1 user=u password=s3cret dbname=d"
        assert mask_literals(f"f($${conninfo}$$)") == "f($$***:dfac0471847d$$)"
        assert mask_literals(f"f('{conninfo}')") == "f('***:dfac0471847d')"

    def test_a_routine_body_mentioning_a_password_column_is_still_untouched(self):
        # The regression this pairs with: restoring the wide predicate here masked the whole body.
        for sql in (
            "$$ BEGIN UPDATE t SET password = other_col; END $$",
            "$$ BEGIN IF password = old THEN RAISE; END IF; END $$",
            "$body$ SELECT password = $1 FROM users $body$",
        ):
            assert mask_literals(sql) == sql

    def test_a_conninfo_with_no_secret_keyword_is_untouched(self):
        sql = "SELECT dblink_connect($$host=db1 user=u dbname=d$$)"
        assert mask_literals(sql) == sql

    def test_an_unterminated_dollar_quote_is_still_judged_and_masked(self):
        # The tokenizer runs an unterminated dollar quote to the end of the input, so the whole
        # remainder is one value; it is masked and left unterminated.
        out = mask_literals("x = $$host=h password=s3cret")
        assert "s3cret" not in out
        assert out.startswith("x = $$") and MASK_PREFIX in out
        assert mask_literals(out) == out

    def test_masking_a_dollar_conninfo_is_idempotent_and_distinct(self):
        once = mask_literals("f($$host=h password=aaa$$)")
        assert mask_literals(once) == once
        assert once != mask_literals("f($$host=h password=bbb$$)")


class TestCommentsKeepTheirText:
    """A comment carries no SQL meaning, so only the offending span is masked. Destroying the
    whole comment costs the reviewer the context the diff exists to show."""

    @pytest.mark.parametrize(
        "sql",
        [
            "-- set password = NULL for disabled users",
            "/* column password = legacy bcrypt; do not reuse */",
            "-- clone with: git clone git://git@github.com/org/repo",
            "-- see https://wiki.example.com/db/password-rotation",
            "/* mail ops@example.com before changing the password */",
            "$$ BEGIN -- password = NULL means disabled\n x(1); END $$",
        ],
        ids=["prose-null", "prose-column", "git-url", "doc-url", "mailto-ish", "in-a-body"],
    )
    def test_a_comment_with_no_credential_is_byte_identical(self, sql):
        assert mask_literals(sql) == sql

    @pytest.mark.parametrize(
        "sql",
        [
            "-- replicated from postgresql://replica1.example.com/app",
            "-- see postgres://reporting@warehouse/app for the read model",
        ],
        ids=["no-userinfo", "user-but-no-password"],
    )
    def test_a_dsn_in_a_comment_with_no_password_keeps_its_text(self, sql):
        # Deliberate, and narrower than ``literals.looks_like_credential_url``: in a comment the
        # host name is context a reviewer needs and there is no secret in it. A secret in the
        # userinfo (``u:s3cret@``) is masked; a userinfo without a ``:`` names an account.
        assert mask_literals(sql) == sql

    def test_only_the_credential_span_of_a_comment_is_replaced(self):
        out = mask_literals("-- rotate password=s3cret then restart the pooler\n")
        assert "s3cret" not in out
        assert out == "-- rotate ***:0c8ea7f07be8 then restart the pooler\n"

    def test_a_quoted_value_with_a_space_is_masked_whole(self):
        # The keyword pass must run before the token scan. The token scan alone masks
        # ``password='P@ss`` and leaves ``word'`` behind, which is the leak the order prevents.
        out = mask_literals("-- conninfo: password='P@ss word' host=db1")
        assert "P@ss" not in out and "word" not in out
        assert out == "-- conninfo: ***:2bcef52e5215 host=db1"

    def test_a_multi_line_block_comment_keeps_its_line_breaks(self):
        out = mask_literals("/* line one\n   dsn postgresql://u:s3cret@h/db\n   line three */")
        assert "s3cret" not in out
        assert out == "/* line one\n   dsn ***:2e8037391e21\n   line three */"

    def test_a_comment_in_a_routine_body_survives_around_the_mask(self):
        out = mask_literals("$$ BEGIN -- dsn postgresql://u:s3cret@h/db\n x(1); END $$")
        assert out == "$$ BEGIN -- dsn ***:2e8037391e21\n x(1); END $$"


class TestEmptyLiterals:
    """The sha256 of the empty string is a published constant, so masking an empty value would
    announce that the password is empty. An empty literal holds no secret."""

    @pytest.mark.parametrize(
        "sql", ["password = ''", "password := ''", "password = $$$$", "password = E''"]
    )
    def test_an_empty_value_after_a_keyword_is_left_alone(self, sql):
        assert mask_literals(sql) == sql

    def test_the_well_known_empty_digest_never_appears(self):
        assert "e3b0c44298fc" not in mask_literals("password = ''")


class TestStability:
    """A digest that differs between runs turns an unchanged value into reported drift, and a
    changed credential into 'no change'. That silent-miss class is the one this tool has already
    shipped once."""

    @pytest.mark.parametrize(
        "sql",
        [
            "password = $$hunter2$$",
            "password = $tag$hunter2$tag$",
            "f($$host=h password=hunter2$$)",
        ],
        ids=["forced-anonymous", "forced-named", "conninfo"],
    )
    def test_remasking_a_dollar_quoted_value_is_a_fixed_point(self, sql):
        once = mask_literals(sql)
        assert "hunter2" not in once
        twice = mask_literals(once)
        assert twice == once
        assert mask_literals(twice) == once

    def test_a_nest_deeper_than_the_cap_does_not_recurse_to_the_limit(self):
        # The depth cap is what keeps a pathological nest from raising RecursionError. 5_000 is
        # far past the cap and far past Python's frame limit, so removing the cap breaks here.
        depth = 5_000
        tags = [f"$t{index}$" for index in range(depth)]
        sql = "".join(tags) + "'postgresql://u:s3cret@h/db'" + "".join(reversed(tags))
        out = mask_literals(sql)
        assert "s3cret" not in out
        assert out.startswith("$t0$") and out.endswith("$t0$")
