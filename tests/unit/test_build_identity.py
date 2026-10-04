"""Routine identity arguments, spelled the same on every server version.

A routine's key includes its argument list, because overloads are distinct objects sharing a name.
That makes the *spelling* of that list load-bearing, and PostgreSQL does not spell it the same way
on every release.
"""

from __future__ import annotations

import pytest

from db_schema_comparer.build import _canonical_identity_arguments as canonical


class TestExplicitInIsDropped:
    """PostgreSQL 13 prints ``p_id integer``; 15 prints ``IN p_id integer``.

    Found by running the integration suite across the version matrix. Left alone, a 13-against-15
    comparison reports every procedure in the database as both missing and extra.
    """

    def test_a_leading_in_is_dropped(self):
        assert canonical("IN p_invoice_id integer") == "p_invoice_id integer"

    def test_both_spellings_converge(self):
        assert canonical("IN p_id integer") == canonical("p_id integer")

    def test_it_is_dropped_from_every_argument(self):
        assert canonical("IN a integer, IN b text") == "a integer, b text"

    def test_a_mixed_list_converges(self):
        assert canonical("IN a integer, b text") == canonical("a integer, b text")


class TestMeaningfulModesSurvive:
    """Only a bare ``IN`` goes. The others change what a caller must pass."""

    def test_inout_is_kept(self):
        assert canonical("INOUT p_id integer") == "INOUT p_id integer"

    def test_variadic_is_kept(self):
        assert canonical("VARIADIC p_values text[]") == "VARIADIC p_values text[]"

    def test_inout_is_kept_alongside_a_dropped_in(self):
        assert canonical("IN a integer, INOUT b text") == "a integer, INOUT b text"


class TestNoFalseMatches:
    @pytest.mark.parametrize(
        "arguments",
        [
            "in_value integer",
            "p_in integer",
            "input text",
            "integer",
            "numeric(10, 2)",
            "p_amount numeric(10, 2), p_name text",
        ],
    )
    def test_an_argument_that_merely_starts_with_in_is_untouched(self, arguments):
        assert canonical(arguments) == arguments

    def test_an_empty_list_stays_empty(self):
        assert canonical("") == ""
        assert canonical(None) == ""
