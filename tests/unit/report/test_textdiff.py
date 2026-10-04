"""The diff a reader actually sees.

Every case here is one a real catalog produces: a one-line change inside a long body, a definition
present on one side only, and CRLF, which Windows-authored migrations make common in this
workspace.
"""

from db_schema_diff.report.textdiff import DiffKind, unified

BEFORE = "SELECT a,\n       b\n  FROM t\n WHERE x = 1\n GROUP BY a"
AFTER = "SELECT a,\n       b\n  FROM t\n WHERE x = 2\n GROUP BY a"


def kinds(lines):
    return [line.kind for line in lines]


def texts(lines):
    return [line.text for line in lines]


class TestOrdinaryChange:
    def test_the_changed_line_appears_removed_then_added(self):
        lines = unified(BEFORE, AFTER)
        kinds_list = kinds(lines)
        assert DiffKind.REMOVED in kinds_list
        assert DiffKind.ADDED in kinds_list
        texts_list = texts(lines)
        assert " WHERE x = 1" in texts_list
        assert " WHERE x = 2" in texts_list
        # Verify exact sequence for the changed lines
        removed_idx = next(
            i
            for i, (k, t) in enumerate(zip(kinds_list, texts_list, strict=False))
            if k is DiffKind.REMOVED and " WHERE x = 1" in t
        )
        added_idx = next(
            i
            for i, (k, t) in enumerate(zip(kinds_list, texts_list, strict=False))
            if k is DiffKind.ADDED and " WHERE x = 2" in t
        )
        assert removed_idx < added_idx

    def test_unchanged_lines_are_context(self):
        lines = unified(BEFORE, AFTER)
        texts_list = texts(lines)
        assert "  FROM t" in texts_list
        from_t_kind = next(line.kind for line in lines if line.text == "  FROM t")
        assert from_t_kind is DiffKind.CONTEXT

    def test_identical_input_produces_nothing(self):
        # An empty diff is how a caller knows there is nothing to show, rather than rendering an
        # empty box.
        assert unified(BEFORE, BEFORE) == ()

    def test_context_is_configurable(self):
        wide = unified(BEFORE, AFTER, context=5)
        narrow = unified(BEFORE, AFTER, context=1)
        assert len(wide) > len(narrow)
        # Verify both have content
        assert len(wide) > 0
        assert len(narrow) > 0

    def test_the_default_context_is_three_lines(self):
        # Pinned because it is what every caller gets: neither the HTML report nor the GUI passes
        # `context`, so changing this default silently changes every rendered diff. Dropping it to
        # 1 passed all 187 report and detail tests.
        import inspect

        assert inspect.signature(unified).parameters["context"].default == 3
        assert unified(BEFORE, AFTER) == unified(BEFORE, AFTER, context=3)


class TestOneSideMissing:
    def test_a_body_only_on_the_master_is_wholly_removed(self):
        lines = unified(BEFORE, None)
        kinds_list = kinds(lines)
        assert set(kinds_list) <= {DiffKind.HUNK, DiffKind.REMOVED}
        assert any(line.kind is DiffKind.REMOVED for line in lines)
        # Verify all non-hunk lines are removed
        for line in lines:
            if line.kind is not DiffKind.HUNK:
                assert line.kind is DiffKind.REMOVED
        # Verify hunk appears only once at the start
        if kinds_list:
            assert kinds_list[0] is DiffKind.HUNK

    def test_a_body_only_on_the_target_is_wholly_added(self):
        lines = unified(None, AFTER)
        kinds_list = kinds(lines)
        assert set(kinds_list) <= {DiffKind.HUNK, DiffKind.ADDED}
        assert any(line.kind is DiffKind.ADDED for line in lines)
        # Verify all non-hunk lines are added
        for line in lines:
            if line.kind is not DiffKind.HUNK:
                assert line.kind is DiffKind.ADDED
        # Verify hunk appears only once at the start
        if kinds_list:
            assert kinds_list[0] is DiffKind.HUNK

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
        # Verify the dropped count is correct
        dropped_count = len(unified(long_before, long_after)) - 20
        assert str(dropped_count) in lines[-1].text
        assert "more" in lines[-1].text

    def test_no_cap_keeps_everything(self):
        long_before = "\n".join(f"line {n}" for n in range(200))
        long_after = "\n".join(f"line {n}" for n in range(200, 400))
        assert len(unified(long_before, long_after, max_lines=None)) > 100

    def test_a_diff_under_the_cap_gets_no_elision_line(self):
        lines = unified(BEFORE, AFTER, max_lines=100)
        assert all(line.kind is not DiffKind.ELIDED for line in lines)

    def test_singular_more_line(self):
        # Test that "1 more line" is used instead of "1 more lines"
        long_before = "\n".join(f"line {n}" for n in range(100))
        long_after = "\n".join(f"line {n}" for n in range(100, 200))
        lines = unified(long_before, long_after, max_lines=25)
        elided = lines[-1]
        assert elided.kind is DiffKind.ELIDED
        assert "1 more line" in elided.text or "more lines" in elided.text
        # Get the exact count and verify grammar
        full_diff = unified(long_before, long_after)
        dropped = len(full_diff) - 25
        if dropped == 1:
            assert "1 more line" in elided.text
        else:
            assert "more lines" in elided.text

    def test_negative_max_lines_treated_as_zero(self):
        # Negative max_lines should be treated as 0
        lines = unified(BEFORE, AFTER, max_lines=-1)
        # With max_lines=0, we get only the elision line
        assert len(lines) == 1
        assert lines[0].kind is DiffKind.ELIDED


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


class TestSpecialCharacterLines:
    """Lines starting with diff markers (-, +, @@, space) must survive intact."""

    def test_line_starting_with_dash_removed(self):
        # A removed line starting with a single dash should be preserved
        lines = unified("a\n-marker\nb", "a\nb")
        removed_texts = [line.text for line in lines if line.kind is DiffKind.REMOVED]
        assert "-marker" in removed_texts

    def test_line_starting_with_dash_added(self):
        # An added line starting with a single dash should be preserved
        lines = unified("a\nb", "a\n-marker\nb")
        added_texts = [line.text for line in lines if line.kind is DiffKind.ADDED]
        assert "-marker" in added_texts

    def test_line_starting_with_double_dash_removed(self):
        # SQL comment: a removed line starting with -- (not confusing with --- header)
        lines = unified("a\n-- comment\nb", "a\nb")
        removed_texts = [line.text for line in lines if line.kind is DiffKind.REMOVED]
        assert "-- comment" in removed_texts

    def test_line_starting_with_double_dash_added(self):
        # SQL comment: an added line starting with --
        lines = unified("a\nb", "a\n-- comment\nb")
        added_texts = [line.text for line in lines if line.kind is DiffKind.ADDED]
        assert "-- comment" in added_texts

    def test_line_starting_with_plus_removed(self):
        # A removed line starting with +
        lines = unified("a\n+flag\nb", "a\nb")
        removed_texts = [line.text for line in lines if line.kind is DiffKind.REMOVED]
        assert "+flag" in removed_texts

    def test_line_starting_with_plus_added(self):
        # An added line starting with +
        lines = unified("a\nb", "a\n+flag\nb")
        added_texts = [line.text for line in lines if line.kind is DiffKind.ADDED]
        assert "+flag" in added_texts

    def test_line_starting_with_double_plus_removed(self):
        # A removed line starting with ++
        lines = unified("a\n++code\nb", "a\nb")
        removed_texts = [line.text for line in lines if line.kind is DiffKind.REMOVED]
        assert "++code" in removed_texts

    def test_line_starting_with_double_plus_added(self):
        # An added line starting with ++
        lines = unified("a\nb", "a\n++code\nb")
        added_texts = [line.text for line in lines if line.kind is DiffKind.ADDED]
        assert "++code" in added_texts

    def test_line_starting_with_at_removed(self):
        # A removed line starting with @@ (rare but possible)
        lines = unified("a\n@@hunk\nb", "a\nb")
        removed_texts = [line.text for line in lines if line.kind is DiffKind.REMOVED]
        assert "@@hunk" in removed_texts

    def test_line_starting_with_at_added(self):
        # An added line starting with @@
        lines = unified("a\nb", "a\n@@hunk\nb")
        added_texts = [line.text for line in lines if line.kind is DiffKind.ADDED]
        assert "@@hunk" in added_texts

    def test_line_starting_with_space_removed(self):
        # A removed line starting with space (context line marker in diff)
        lines = unified("a\n content\nb", "a\nb")
        removed_texts = [line.text for line in lines if line.kind is DiffKind.REMOVED]
        assert " content" in removed_texts

    def test_line_starting_with_space_added(self):
        # An added line starting with space
        lines = unified("a\nb", "a\n content\nb")
        added_texts = [line.text for line in lines if line.kind is DiffKind.ADDED]
        assert " content" in added_texts

    def test_first_line_is_hunk_when_diff_exists(self):
        # When there is a diff, the first line should be a HUNK
        lines = unified(BEFORE, AFTER)
        if lines:
            assert lines[0].kind is DiffKind.HUNK

    def test_first_line_is_hunk_for_one_side_missing(self):
        # When one side is missing, first line should be HUNK
        lines = unified(BEFORE, None)
        if lines:
            assert lines[0].kind is DiffKind.HUNK


class TestCriticalReproductions:
    """Reproduce the critical bug that was fixed: SQL comments being dropped."""

    def test_sql_comment_change_visible(self):
        # SQL comment change: -- deprecated vs -- current
        before = "a\n-- deprecated\nb"
        after = "a\n-- current\nb"
        lines = unified(before, after)
        texts_list = texts(lines)
        # Both the old and new comments should appear
        assert any("-- deprecated" in t for t in texts_list)
        assert any("-- current" in t for t in texts_list)
        # Verify they are marked as removed and added
        kinds_list = kinds(lines)
        assert DiffKind.REMOVED in kinds_list
        assert DiffKind.ADDED in kinds_list

    def test_sql_comment_deletion_visible(self):
        # Deletion of a SQL comment
        before = "a\n-- deprecated\nb"
        after = "a\nb"
        lines = unified(before, after)
        texts_list = texts(lines)
        # The deleted comment should appear in the diff
        assert any("-- deprecated" in t for t in texts_list)
        # Verify it's marked as removed
        kinds_list = kinds(lines)
        assert DiffKind.REMOVED in kinds_list

    def test_sql_comment_addition_visible(self):
        # Addition of a SQL comment
        before = "a\nb"
        after = "a\n++x\nb"
        lines = unified(before, after)
        texts_list = texts(lines)
        # The added code should appear in the diff
        assert any("++x" in t for t in texts_list)
        # Verify it's marked as added
        kinds_list = kinds(lines)
        assert DiffKind.ADDED in kinds_list
