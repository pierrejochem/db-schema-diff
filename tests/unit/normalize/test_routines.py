"""Routine body canonicalization.

Whitespace is normalised; nothing else is. A plpgsql body is full of string literals, and touching
their content would change what the function says.
"""

from __future__ import annotations

import pytest

from db_schema_comparer.normalize.routines import (
    bodies_equivalent,
    body_hash,
    canonical_body,
)

BODY = """
DECLARE
    v_total numeric;
BEGIN
    SELECT amount INTO v_total FROM t WHERE id = p_id;
    RETURN v_total;
END;
"""


class TestWhitespace:
    def test_windows_line_endings_are_normalised(self):
        # Migrations authored on Windows are common in this platform, and a line ending is not
        # behaviour.
        assert bodies_equivalent(BODY, BODY.replace("\n", "\r\n"))

    def test_lone_carriage_returns_are_normalised(self):
        assert bodies_equivalent(BODY, BODY.replace("\n", "\r"))

    def test_indentation_depth_does_not_matter(self):
        reindented = "\n".join("        " + line.strip() for line in BODY.split("\n"))
        assert bodies_equivalent(BODY, reindented)

    def test_blank_lines_are_dropped(self):
        assert bodies_equivalent(BODY, BODY.replace("BEGIN", "BEGIN\n\n\n"))

    def test_trailing_whitespace_is_dropped(self):
        assert bodies_equivalent(BODY, BODY.replace("BEGIN", "BEGIN   "))

    def test_runs_of_spaces_collapse(self):
        assert bodies_equivalent(BODY, BODY.replace("SELECT amount", "SELECT      amount"))

    def test_tabs_and_spaces_are_interchangeable(self):
        assert bodies_equivalent("BEGIN\n\tRETURN 1;\nEND;", "BEGIN\n    RETURN 1;\nEND;")


class TestMeaningIsPreserved:
    def test_case_is_never_folded(self):
        """A plpgsql body is full of string literals.

        Lower-casing ``RAISE EXCEPTION 'Invoice % not found'`` would change the message the function
        actually produces.
        """
        assert not bodies_equivalent("RAISE 'Invoice not found'", "raise 'invoice not found'")

    def test_whitespace_inside_a_literal_is_significant(self):
        # Collapsing it would change the string the function returns.
        assert not bodies_equivalent("RETURN 'a  b';", "RETURN 'a b';")

    def test_a_different_body_is_a_different_body(self):
        assert not bodies_equivalent(BODY, BODY.replace("RETURN v_total", "RETURN 0"))

    def test_a_removed_statement_is_detected(self):
        assert not bodies_equivalent(BODY, BODY.replace("    RETURN v_total;\n", ""))


class TestHash:
    def test_the_hash_is_a_sha256_hex_digest(self):
        digest = body_hash(BODY)
        assert digest is not None
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)

    def test_equivalent_bodies_hash_the_same(self):
        assert body_hash(BODY) == body_hash(BODY.replace("\n", "\r\n"))

    def test_different_bodies_hash_differently(self):
        assert body_hash(BODY) != body_hash(BODY.replace("RETURN v_total", "RETURN 0"))

    def test_the_hash_is_stable_across_calls(self):
        assert body_hash(BODY) == body_hash(BODY)


class TestAbsentBody:
    @pytest.mark.parametrize("value", [None])
    def test_no_body_hashes_to_none(self, value):
        # An aggregate or window function has no extractable body; those are compared on their
        # declared attributes alone.
        assert body_hash(value) is None
        assert canonical_body(value) is None

    def test_an_empty_body_canonicalizes_to_an_empty_string(self):
        assert canonical_body("   \n\n  ") == ""
        assert body_hash("") is not None
