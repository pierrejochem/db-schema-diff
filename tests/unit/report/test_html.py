"""The standalone HTML report.

The two assertions that matter most are negative ones: the file references nothing external, and it
leaks no credential. A drift report is read from a CI artifact, often on a machine with no outbound
access, and often by someone other than whoever ran it.
"""

from __future__ import annotations

import io
import json
import pathlib
import re

import pytest

from db_schema_comparer.report.html import HtmlReporter, load_template_source
from db_schema_comparer.report.json_report import JsonReporter
from tests.support.reports import HOSTILE_TEXT, clean_report, full_report


def render(report=None) -> str:
    out = io.StringIO()
    HtmlReporter().render(report or full_report(), out)
    return out.getvalue()


@pytest.fixture(scope="module")
def html() -> str:
    return render()


class TestSelfContained:
    """Nothing outside the file, ever."""

    # Markup, not text: definition text is on the page, and a routine body or SQL comment may
    # legitimately mention a URL. What must never happen is the page *loading* something.
    def test_no_link_element_and_no_import(self, html):
        assert "<link" not in html
        assert "@import" not in html.split("</style>")[0]
        assert "url(" not in html.split("</style>")[0]

    def test_no_loading_attribute(self, html):
        assert not re.search(r"\saction\s*=", html)

    def test_a_url_in_a_definition_is_not_an_external_reference(self):
        from tests.unit.report.test_html_diff import report_with_a_url_in_a_body

        page = render(report_with_a_url_in_a_body())
        assert "http://example.com/docs" in page
        assert "<link" not in page
        assert not re.search(r"\s(src|href|action)\s*=", page)

    def test_no_element_loads_anything(self, html):
        # src= and href= are how an external resource gets in; neither may appear at all.
        assert not re.search(r"\ssrc\s*=", html)
        assert not re.search(r"\shref\s*=", html)

    def test_the_template_itself_references_nothing_external(self):
        # Checked separately, so a URL cannot hide in a branch this report happens not to exercise.
        source = load_template_source()
        for pattern in ("http://", "https://", "//cdn", "@import", "src=", "href="):
            assert pattern not in source

    def test_the_styles_are_inline(self, html):
        assert "<style>" in html

    def test_the_icons_are_inline_svg(self, html):
        assert "<svg" in html

    def test_it_declares_a_doctype_and_a_charset(self, html):
        assert html.lstrip().startswith("<!doctype html>")
        assert '<meta charset="utf-8">' in html


class TestNoCredentials:
    def test_no_credential_appears_anywhere(self, html):
        for forbidden in ("password", "dsn_env", "PROD_DSN", "postgresql://"):
            assert forbidden not in html

    def test_the_embedded_payload_carries_no_credential(self, html):
        payload = _payload(html)
        assert "password" not in json.dumps(payload)


class TestJsonParity:
    """The embedded payload is the JSON reporter's output, byte for byte.

    That is what stops the two formats drifting apart: a field added to one and forgotten in the
    other fails here.
    """

    def test_the_embedded_payload_matches_the_json_reporter(self):
        report = full_report()
        out = io.StringIO()
        JsonReporter().render(report, out)
        assert _payload(render(report)) == json.loads(out.getvalue())

    def test_the_payload_is_valid_json(self, html):
        assert _payload(html)["name"] == "invoicing"

    def test_the_escaping_keeps_the_json_valid(self):
        # \u003c is a legal JSON escape, so escaping every `<` costs nothing but ugliness.
        report = full_report()
        assert _payload(render(report)) == json.loads(json.dumps(report.to_json_dict()))


class TestScriptInjection:
    def test_a_closing_script_tag_in_the_data_cannot_break_out(self):
        """A value containing ``</script>`` must not close the element early.

        Column defaults, check expressions and function bodies all reach the payload, and any of
        them could contain that sequence — deliberately or by accident.
        """
        from db_schema_comparer.diff.model import (
            AttributeDelta,
            ComparisonReport,
            ObjectFinding,
            ObjectStatus,
            TargetDiff,
        )
        from db_schema_comparer.diff.severity import Severity
        from db_schema_comparer.model.keys import column_key

        hostile = "</script><script>alert(1)</script>"
        report = ComparisonReport(
            name="invoicing",
            master_label="prod",
            targets=(
                TargetDiff(
                    master_label="prod",
                    target_label="qa",
                    master_source=None,
                    target_source=None,
                    findings=(
                        ObjectFinding(
                            key=column_key("public", "t", "c"),
                            status=ObjectStatus.DIFFERS,
                            severity=Severity.ERROR,
                            deltas=(
                                AttributeDelta(
                                    attribute="column.default",
                                    master_value=hostile,
                                    target_value="'x'",
                                    severity=Severity.ERROR,
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )
        html = render(report)
        # No executable script element may appear anywhere.
        assert "<script>alert(1)</script>" not in html
        # And no stray `<` inside the payload either: a `<!--<script` sequence would put the HTML
        # tokenizer into an escaped state where a later `</script>` does not close the element.
        assert html.count("<script") == html.count("</script>")
        # The payload must still be valid, readable JSON with the value intact.
        assert _payload(html)["targets"][0]["findings"][0]["deltas"][0]["master"] == hostile

    def test_markup_in_a_value_is_escaped_in_the_body(self, html):
        # The fixture's hostile value contains a control character, not markup, but the principle is
        # the same: nothing from a database is emitted unescaped.
        assert "<b>" not in html


class TestContent:
    def test_the_verdict_leads(self, html):
        # A partial comparison is never presented as a clean one.
        assert "could not be inspected" in html
        assert 'class="verdict error"' in html

    def test_a_clean_report_says_so(self):
        html = render(clean_report())
        assert 'class="verdict ok"' in html
        assert "No differences found" in html

    def test_every_target_appears(self, html):
        for label in ("qa", "dev", "local"):
            assert f">{label}</h3>" in html

    def test_the_matrix_has_a_column_per_target(self, html):
        header = html.split('<table class="matrix">')[1].split("</thead>")[0]
        for label in ("qa", "dev", "local"):
            assert f">{label}</th>" in header

    def test_the_matrix_has_a_row_per_kind_that_produced_a_finding(self, html):
        body = html.split("<tbody>")[1].split("</tbody>")[0]
        assert ">tables</th>" in body
        assert ">columns</th>" in body
        # Nothing produced a sequence finding, so that row is absent rather than a row of zeros.
        assert ">sequences</th>" not in body

    def test_an_unreachable_target_shows_a_dash_not_a_zero(self, html):
        # A zero would read as "nothing wrong here"; it was never looked at.
        assert 'class="count skipped"' in html

    def test_findings_carry_their_path_and_severity(self, html):
        assert 'data-path="cumo-invoicing.invoice.number"' in html
        assert 'data-severity="error"' in html

    def test_a_delta_shows_both_values_and_its_reason(self, html):
        assert "varchar(40)" in html
        assert "relying on the length" in html

    def test_suppressed_findings_are_listed_under_their_rule(self, html):
        assert "quartz-runtime" in html
        assert "set aside by ignore rules" in html

    def test_the_hostile_fixture_value_survives_escaping(self, html):
        assert HOSTILE_TEXT.replace("\x01", "") in html or HOSTILE_TEXT in html


class TestWorksWithoutJavaScript:
    def test_expanding_uses_details_not_script(self, html):
        # Every finding must be reachable with scripting off.
        assert "<details" in html
        assert "onclick" not in html

    def test_a_target_with_findings_is_open_by_default(self, html):
        assert '<details class="target" open>' in html

    def test_findings_are_rendered_into_the_dom(self, html):
        # Not generated from the payload, so nothing depends on the script running.
        body = html.split('<script type="application/json"')[0]
        assert "cumo-invoicing.invoice.number" in body


class TestPresentation:
    def test_dark_mode_is_supported(self, html):
        assert "prefers-color-scheme: dark" in html

    def test_print_is_supported(self, html):
        assert "@media print" in html

    def test_it_is_usable_on_a_phone(self, html):
        assert 'name="viewport"' in html

    def test_the_title_names_the_comparison(self, html):
        assert "<title>invoicing — schema comparison</title>" in html


class TestChangelogSection:
    def test_the_changelog_appears_when_it_is_applicable(self):
        from db_schema_comparer.diff.changelog import ChangelogDiff, ChangelogStatus
        from db_schema_comparer.diff.model import ComparisonReport, TargetDiff
        from db_schema_comparer.diff.severity import Severity
        from tests.support.builders import source

        report = ComparisonReport(
            name="invoicing",
            master_label="prod",
            targets=(
                TargetDiff(
                    master_label="prod",
                    target_label="qa",
                    master_source=source("prod"),
                    target_source=source("qa"),
                    changelog=ChangelogDiff(
                        status=ChangelogStatus.TARGET_BEHIND,
                        severity=Severity.ERROR,
                        missing_in_target=(("add-invoice-line", "kolowae"),),
                        first_divergence=("add-invoice-line", "kolowae"),
                        master_tag="R7.6.2",
                    ),
                ),
            ),
        )
        html = render(report)
        assert "Liquibase" in html
        assert "1 changeset(s) behind" in html
        assert "histories diverge at" in html
        assert ">liquibase</th>" in html

    def test_it_is_absent_when_neither_database_uses_liquibase(self, html):
        # The fixture has no changelog, so the section and its matrix row must not appear at all.
        assert ">liquibase</th>" not in html


class TestDeterminism:
    def test_two_renders_are_byte_identical(self):
        assert render() == render()


def _payload(html: str) -> dict:
    """The embedded JSON block, parsed."""
    marker = '<script type="application/json" id="report-data">'
    start = html.index(marker) + len(marker)
    end = html.index("</script>", start)
    return json.loads(html[start:end])


class TestHtmlValidity:
    """Structural mistakes a browser silently repairs, and which then break the styling.

    A ``<dl>`` inside a ``<p>`` is the one that bit: the parser closes the paragraph early, so any
    rule scoped to ``.meta dl`` stops matching and the metadata renders as a stacked list. Nothing
    errors; it just looks wrong, which is exactly the kind of thing a test has to catch.
    """

    #: Elements that may not contain flow content, so a browser closes them early.
    def test_no_definition_list_inside_a_paragraph(self, html):
        import re

        for paragraph in re.findall(r"<p\b[^>]*>(.*?)</p>", html, re.DOTALL):
            assert "<dl" not in paragraph
            assert "<table" not in paragraph
            assert "<details" not in paragraph
            assert "<ul" not in paragraph

    def test_tags_are_balanced(self, html):
        from html.parser import HTMLParser

        void = {
            "meta",
            "br",
            "hr",
            "img",
            "input",
            "link",
            "area",
            "base",
            "col",
            "embed",
            "source",
            "track",
            "wbr",
        }

        class Balance(HTMLParser):
            def __init__(self) -> None:
                super().__init__(convert_charrefs=True)
                self.stack: list[str] = []
                self.problems: list[str] = []

            def handle_starttag(self, tag, attrs):
                if tag not in void:
                    self.stack.append(tag)

            def handle_endtag(self, tag):
                if tag in void:
                    return
                if not self.stack or self.stack[-1] != tag:
                    self.problems.append(f"</{tag}> does not match <{self.stack[-1:]}>")
                    return
                self.stack.pop()

        parser = Balance()
        parser.feed(html)
        assert parser.problems == []
        assert parser.stack == []


class TestNarrowScreens:
    def test_the_matrix_scrolls_rather_than_overflowing_the_page(self, html):
        # It gains a column per target, so at phone width it must scroll inside its own box.
        assert '<div class="matrix-wrap">' in html
        assert ".matrix-wrap { overflow-x: auto; }" in html


class TestFilterWiring:
    """The filter script and the markup have to agree on their hooks.

    The behaviour itself is verified in a real browser; this only guards against a rename in one
    place and not the other, which would break filtering silently.
    """

    def test_the_script_targets_elements_that_exist(self, html):
        script = html.split("<script>")[-1]
        for element_id in ("filter", "hits"):
            assert f'id="{element_id}"' in html
            assert f'"{element_id}"' in script

    def test_the_script_does_not_depend_on_the_embedded_payload(self, html):
        """The payload is there so the file is self-describing, not to drive the page.

        Findings are rendered into the DOM, so filtering works on what is already there and the page
        is complete with scripting off.
        """
        assert 'id="report-data"' in html
        assert "report-data" not in html.split("<script>")[-1]

    def test_every_finding_carries_the_attributes_the_script_reads(self, html):
        import re

        findings = re.findall(r"<li([^>]*data-path[^>]*)>", html)
        assert findings
        for attributes in findings:
            assert "data-severity=" in attributes

    def test_a_severity_checkbox_exists_for_each_level(self, html):
        for level in ("error", "warning", "info"):
            assert f'class="sev" value="{level}"' in html


# The report and the GUI are one design system, so they are held to one palette.
TOKEN_TO_CSS_VARIABLE = {
    "success": "ok",
    "success-surface": "ok-bg",
    "danger": "error",
    "danger-surface": "error-bg",
    "caution": "warning",
    "caution-surface": "warning-bg",
    "info": "info",
    "info-surface": "info-bg",
    "body": "text",
    "muted": "muted",
    "hairline": "border",
    "card": "bg",
    "sunken": "panel",
    "heading": "heading",
    "accent": "accent",
}


def design_tokens() -> dict[str, str]:
    """The GUI's colour tokens, read as text — this runs where Slint is not installed."""
    import db_schema_comparer

    source = (
        pathlib.Path(db_schema_comparer.__file__).parent / "gui" / "ui" / "tokens.slint"
    ).read_text(encoding="utf-8")
    return dict(re.findall(r"out property <color> (\S+): (#[0-9a-fA-F]+);", source))


def light_scheme() -> dict[str, str]:
    """The template's light `:root` block. The dark overrides live inside a media query."""
    source = load_template_source()
    block = source[source.index(":root {") : source.index("}", source.index(":root {"))]
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]+);", block))


@pytest.mark.parametrize(("token", "variable"), sorted(TOKEN_TO_CSS_VARIABLE.items()))
def test_the_report_and_the_gui_agree_on_every_colour(token, variable):
    """One palette, two renderers.

    A report says "error" in the same breath as the window that produced it, and a reader moving
    between them should not have to learn two colour languages. Nothing stops the two drifting
    except this, because each side renders correctly on its own.
    """
    tokens = design_tokens()
    assert token in tokens, f"{token} is no longer a GUI token; update the mapping"
    assert light_scheme()[variable] == tokens[token]


def test_the_brand_faces_are_named_but_never_fetched(html):
    # The report may ask for the brand faces; it may not ship or download them. The no-external-URL
    # test covers the fetch; this pins the naming, so a reader who has them sees them.
    assert "Mulish" in html
    assert "Archivo" in html
    assert "IBM Plex Mono" in html
    assert "@font-face" not in html
    assert "base64" not in html
