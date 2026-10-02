"""The shared artifact shows what changed, and still carries no network and no secret."""

import io
import re

from cumo_schema_comparer.report.console import ConsoleReporter
from cumo_schema_comparer.report.html import HtmlReporter


def render(report):
    out = io.StringIO()
    HtmlReporter().render(report, out)
    return out.getvalue()


def test_a_body_delta_renders_a_diff(a_report_with_changed_view):
    html = render(a_report_with_changed_view)
    assert 'class="diff"' in html
    assert "diff-removed" in html
    assert "diff-added" in html


def test_a_scalar_delta_renders_no_diff(a_report_with_changed_column_type):
    html = render(a_report_with_changed_column_type)
    assert 'class="diff"' not in html


def test_the_diff_is_capped_with_a_count(a_report_with_a_huge_body_change):
    html = render(a_report_with_a_huge_body_change)
    assert re.search(r"… \d+ more lines", html)
    assert html.count('<span class="diff-') <= 61


def test_the_payload_still_carries_the_full_values(a_report_with_a_huge_body_change):
    # The cap is a rendering decision. The embedded JSON stays lossless, which is what the
    # "the two outputs cannot drift apart" test depends on.
    html = render(a_report_with_a_huge_body_change)
    assert html.count("line 199") >= 1


def test_the_page_still_references_nothing_external(a_report_with_changed_view):
    html = render(a_report_with_changed_view)
    for pattern in ("http://", "https://", "//cdn"):
        assert pattern not in html


def test_angle_brackets_in_a_body_are_escaped(a_report_with_angle_brackets_in_a_body):
    html = render(a_report_with_angle_brackets_in_a_body)
    # The page has a <script> of its own, so look for the injected text rather than the tag.
    assert "<script>alert" not in html
    assert "<script>x" not in html
    assert "&lt;script&gt;alert(1)" in html


def test_a_routine_without_a_captured_body_renders_no_diff(a_report_with_a_hashed_routine):
    # Diffing two hashes would be meaningless; the before/after row is the right rendering.
    html = render(a_report_with_a_hashed_routine)
    assert 'class="diff"' not in html
    assert "a" * 64 in html
    assert "b" * 64 in html


def test_a_routine_with_a_captured_body_diffs_the_text_not_the_hashes(
    a_report_with_a_captured_routine,
):
    html = render(a_report_with_a_captured_routine)
    assert 'class="diff"' not in html  # short, single-line: the row says it better
    assert "BEGIN RETURN 2; END" in html


def test_a_long_one_line_routine_body_diffs_its_text(a_report_with_a_50kb_one_line_body):
    html = render(a_report_with_a_50kb_one_line_body)
    assert 'class="diff"' in html
    assert "diff-removed" in html and "diff-added" in html
    diff_block = html.split('<pre class="diff">')[1].split("</pre>")[0]
    assert "a" * 64 not in diff_block


def test_masked_values_diff_normally(a_report_with_masked_values):
    html = render(a_report_with_masked_values)
    assert 'class="diff"' in html
    assert "***:a3f1c9" in html
    assert "***:b7c2d0" in html


def test_a_large_multiline_body_stays_bounded(a_report_with_a_50kb_multiline_body):
    html = render(a_report_with_a_50kb_multiline_body)
    assert re.search(r"… \d+ more lines", html)
    assert html.count('<span class="diff-') <= 61
    assert len(html) < 400_000


def test_the_console_reporter_shows_no_diff(a_report_with_a_huge_body_change):
    """Deliberate divergence: CI logs stay readable. The renderings must agree on the verdict,
    which they do; detail depth has always differed."""
    out = io.StringIO()
    ConsoleReporter().render(a_report_with_a_huge_body_change, out)
    rendered = out.getvalue()
    assert "@@" not in rendered
    assert "more lines" not in rendered
