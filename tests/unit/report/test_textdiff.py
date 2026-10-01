"""The diff a reader actually sees.

Every case here is one a real catalog produces: a one-line change inside a long body, a definition
present on one side only, and CRLF, which Windows-authored migrations make common in this
workspace.
"""

from cumo_schema_comparer.report.textdiff import DiffKind, unified

BEFORE = "SELECT a,\n       b\n  FROM t\n WHERE x = 1\n GROUP BY a"
AFTER = "SELECT a,\n       b\n  FROM t\n WHERE x = 2\n GROUP BY a"


def kinds(lines):
    return [line.kind for line in lines]


def texts(lines):
    return [line.text for line in lines]


class TestOrdinaryChange:
    def test_the_changed_line_appears_removed_then_added(self):
        lines = unified(BEFORE, AFTER)
        assert DiffKind.REMOVED in kinds(lines)
        assert DiffKind.ADDED in kinds(lines)
        assert " WHERE x = 1" in texts(lines)
        assert " WHERE x = 2" in texts(lines)

    def test_unchanged_lines_are_context(self):
        lines = unified(BEFORE, AFTER)
        assert "  FROM t" in texts(lines)
        assert next(line.kind for line in lines if line.text == "  FROM t") is DiffKind.CONTEXT

    def test_identical_input_produces_nothing(self):
        # An empty diff is how a caller knows there is nothing to show, rather than rendering an
        # empty box.
        assert unified(BEFORE, BEFORE) == ()

    def test_context_is_configurable(self):
        wide = unified(BEFORE, AFTER, context=5)
        narrow = unified(BEFORE, AFTER, context=1)
        assert len(wide) > len(narrow)


class TestOneSideMissing:
    def test_a_body_only_on_the_master_is_wholly_removed(self):
        lines = unified(BEFORE, None)
        assert set(kinds(lines)) <= {DiffKind.HUNK, DiffKind.REMOVED}
        assert any(line.kind is DiffKind.REMOVED for line in lines)

    def test_a_body_only_on_the_target_is_wholly_added(self):
        lines = unified(None, AFTER)
        assert set(kinds(lines)) <= {DiffKind.HUNK, DiffKind.ADDED}
        assert any(line.kind is DiffKind.ADDED for line in lines)

    def test_both_missing_produces_nothing(self):
        assert unified(None, None) == ()


class TestLineEndings:
    def test_crlf_against_lf_is_not_a_whole_file_change(self):
        """Otherwise every Windows-authored routine reports every line as drift."""
        crlf = BEFORE.replace("\n", "\r\n")
        assert unified(crlf, BEFORE) == ()

    def test_a_real_change_still_shows_through_crlf(self):
        crlf = BEFORE.replace("\n", "\r\n")
        lines = unified(crlf, AFTER)
        assert " WHERE x = 2" in texts(lines)

    def test_a_trailing_newline_difference_alone_is_not_a_change(self):
        assert unified(BEFORE, BEFORE + "\n") == ()


class TestCap:
    def test_the_cap_truncates_and_says_how_many_were_dropped(self):
        long_before = "\n".join(f"line {n}" for n in range(200))
        long_after = "\n".join(f"line {n}" for n in range(200, 400))
        lines = unified(long_before, long_after, max_lines=20)
        assert len(lines) == 21
        assert lines[-1].kind is DiffKind.ELIDED
        assert "more" in lines[-1].text

    def test_no_cap_keeps_everything(self):
        long_before = "\n".join(f"line {n}" for n in range(200))
        long_after = "\n".join(f"line {n}" for n in range(200, 400))
        assert len(unified(long_before, long_after, max_lines=None)) > 100

    def test_a_diff_under_the_cap_gets_no_elision_line(self):
        lines = unified(BEFORE, AFTER, max_lines=100)
        assert all(line.kind is not DiffKind.ELIDED for line in lines)


class TestAwkwardText:
    def test_a_very_long_single_line_is_one_line(self):
        lines = unified("x" * 5000, "y" * 5000)
        assert sum(1 for line in lines if line.kind is DiffKind.REMOVED) == 1

    def test_unicode_survives(self):
        lines = unified("SELECT 'Übergröße'", "SELECT 'Ubergrosse'")
        assert any("Übergröße" in line.text for line in lines)

    def test_an_empty_string_is_not_the_same_as_none(self):
        # An empty body and an absent body are different facts about the database.
        assert unified("", "a") != ()
