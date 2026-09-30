"""Editing an ignore ruleset.

The pick-lists matter as much as the editing: an ignore rule with an invalid value is refused by the
validator, and a form that can produce one wastes the user's time.
"""

from __future__ import annotations

import textwrap

import pytest

from cumo_schema_comparer.gui.config_vm import ConfigDocument
from cumo_schema_comparer.gui.errors import GuiError
from cumo_schema_comparer.gui.ignores_vm import (
    RULE_ACTIONS,
    RULE_STATUSES,
    IgnoresDocument,
    rule_kinds,
)

RULES = textwrap.dedent(
    """
    version: 1
    options:
      case_insensitive_globs: true
    rules:
    - id: audit-archive
      reason: created by a nightly job in production only
      names:
      - audit_archive.*
    - id: dev-experiments
      targets:
      - dev
      kinds:
      - index
      statuses:
      - extra_in_target
    """
).lstrip()


@pytest.fixture
def document(tmp_path) -> IgnoresDocument:
    path = tmp_path / "ignores.yaml"
    path.write_text(RULES)
    config_path = tmp_path / "invoicing.yaml"
    config_path.write_text(
        "version: 1\nname: invoicing\n"
        "master:\n  label: prod\n  dsn_env: P\n"
        "targets:\n- label: qa\n  dsn_env: Q\n"
        "ignores_file: ignores.yaml\n"
    )
    return IgnoresDocument.for_config(ConfigDocument.load(config_path))


class TestPickLists:
    def test_statuses_offer_only_what_the_validator_accepts(self):
        """ObjectStatus has four members; IgnoreRule accepts three.

        Building the list from ObjectStatus would offer `match`, which the validator rejects — a
        value the form should not be able to produce.
        """
        assert RULE_STATUSES == ("missing_in_target", "extra_in_target", "differs")
        assert "match" not in RULE_STATUSES

    def test_kinds_come_from_the_object_kinds(self):
        from cumo_schema_comparer.model.kinds import ObjectKind

        assert set(rule_kinds()) == {kind.value for kind in ObjectKind}

    def test_actions_are_the_three_the_model_allows(self):
        assert RULE_ACTIONS == ("ignore", "warn", "info")

    @pytest.mark.parametrize("status", RULE_STATUSES)
    def test_every_offered_status_validates(self, status):
        from cumo_schema_comparer.config.model import IgnoreRule

        IgnoreRule.model_validate({"id": "r", "statuses": [status]})

    @pytest.mark.parametrize("kind", rule_kinds())
    def test_every_offered_kind_validates(self, kind):
        from cumo_schema_comparer.config.model import IgnoreRule

        IgnoreRule.model_validate({"id": "r", "kinds": [kind]})


class TestEditing:
    def test_rule_rows_expose_the_project_rules(self, document):
        rows = document.rule_rows()
        assert [row["id"] for row in rows] == ["audit-archive", "dev-experiments"]
        assert rows[1]["targets"] == ["dev"]
        assert rows[1]["action"] == "ignore"

    def test_the_bundled_defaults_are_shown_read_only(self, document):
        """A reader should see what is already suppressed without opening the package."""
        defaults = document.default_rows()
        assert {row["id"] for row in defaults} >= {
            "quartz-runtime",
            "liquibase-bookkeeping",
            "scratch-objects",
        }
        assert all(row["read_only"] is True for row in defaults)

    def test_updating_a_rule_marks_the_document_dirty(self, document):
        document.update_rule("dev-experiments", "action", "warn")
        assert document.dirty is True
        assert document.config.rules[1].action == "warn"

    def test_adding_and_removing_a_rule(self, document):
        document.add_rule("new-rule")
        assert [r.id for r in document.config.rules][-1] == "new-rule"
        document.remove_rule("audit-archive")
        assert "audit-archive" not in [r.id for r in document.config.rules]

    def test_a_duplicate_rule_id_is_refused(self, document):
        with pytest.raises(GuiError, match="audit-archive"):
            document.add_rule("audit-archive")

    def test_a_new_rule_starts_restricted_enough_to_validate(self, document):
        # A rule restricting nothing would suppress the whole report and is refused by the model.
        document.add_rule("new-rule")
        assert document.validate() == []


class TestValidation:
    def test_an_unbounded_rule_is_reported_against_its_field(self, document):
        document.update_rule("audit-archive", "names", [])
        errors = document.validate()
        assert errors
        assert any("every finding" in str(error) for error in errors)

    def test_errors_are_gui_errors(self, document):
        document.update_rule("audit-archive", "names", [])
        assert all(isinstance(error, GuiError) for error in document.validate())


class TestBothSourcesDeclared:
    """Review Focus 2: `ignores_file` and an inline `ignores` block together.

    The loader raises a ConfigError with no field path. The UI has to attach it to the Ignores tab
    rather than show an error belonging to nothing.
    """

    def test_declaring_both_is_reported_against_the_ignores_tab(self, tmp_path):
        config_path = tmp_path / "invoicing.yaml"
        config_path.write_text(
            "version: 1\nname: invoicing\n"
            "master:\n  label: prod\n  dsn_env: P\n"
            "targets:\n- label: qa\n  dsn_env: Q\n"
            "ignores_file: ignores.yaml\n"
            "ignores:\n  version: 1\n  rules:\n  - id: inline\n    names: ['a.b']\n"
        )
        (tmp_path / "ignores.yaml").write_text(RULES)

        with pytest.raises(GuiError) as exc:
            IgnoresDocument.for_config(ConfigDocument.load(config_path))
        assert "not both" in str(exc.value)
        assert exc.value.field == "ignores"

    def test_neither_declared_starts_from_an_empty_ruleset(self, tmp_path):
        config_path = tmp_path / "invoicing.yaml"
        config_path.write_text(
            "version: 1\nname: invoicing\n"
            "master:\n  label: prod\n  dsn_env: P\n"
            "targets:\n- label: qa\n  dsn_env: Q\n"
        )
        document = IgnoresDocument.for_config(ConfigDocument.load(config_path))
        assert document.config.rules == ()
        assert document.default_rows()  # the bundled ones are still shown


class TestRulesThatCanNeverMatch:
    """A rule that matches nothing reads as coverage the team does not have."""

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("kinds", ["tabel"]),
            ("statuses", ["match"]),
            ("statuses", ["secret-value"]),
            ("action", "silence"),
            ("attributes", ["column.collation"]),  # kinds is [index] on rule 2, below
        ],
    )
    def test_reported_without_echoing_the_value(self, document, field, value):
        target = "dev-experiments"
        document.update_rule(target, field, value)
        errors = document.validate()
        assert errors
        assert all(e.field and e.field.startswith("rules.1") for e in errors)
        for error in errors:
            assert "tabel" not in str(error)
            assert "secret-value" not in str(error)
            assert "silence" not in str(error)

    def test_attribute_rule_excluding_differs_is_reported(self, document):
        document.update_rule("audit-archive", "attributes", ["column.collation"])
        document.update_rule("audit-archive", "statuses", ["missing_in_target"])
        assert any("differs" in str(e) for e in document.validate())

    def test_attribute_of_another_kind_is_reported(self, document):
        document.update_rule("dev-experiments", "attributes", ["column.collation"])
        document.update_rule("dev-experiments", "statuses", ["differs"])
        assert any(e.field == "rules.1.attributes" for e in document.validate())

    def test_duplicate_ids_reported_by_position(self, document):
        document.update_rule("dev-experiments", "id", "audit-archive")
        errors = document.validate()
        assert [e.field for e in errors] == ["rules.1.id"]
        assert "audit-archive" not in str(errors[0])

    def test_an_invalid_ruleset_is_not_written(self, document, tmp_path):
        before = document.path.read_text()
        document.update_rule("audit-archive", "kinds", ["nope"])
        with pytest.raises(GuiError):
            document.save()
        with pytest.raises(GuiError):
            document.to_yaml()
        assert document.path.read_text() == before


class TestSaving:
    def test_untouched_round_trip_is_byte_identical(self, document):
        indented = document.to_yaml()
        document.path.write_text(indented)
        reloaded = IgnoresDocument.for_config(
            ConfigDocument.load(document.path.parent / "invoicing.yaml")
        )
        assert reloaded.to_yaml() == indented
        assert reloaded.config == document.config

    def test_save_writes_and_clears_dirty(self, document):
        document.update_rule("dev-experiments", "action", "warn")
        document.save()
        assert document.dirty is False
        assert "action: warn" in document.path.read_text()

    def test_save_without_a_path_is_refused(self, tmp_path):
        config_path = tmp_path / "c.yaml"
        config_path.write_text(
            "version: 1\nname: c\nmaster:\n  label: p\n  dsn_env: P\n"
            "targets:\n- label: q\n  dsn_env: Q\n"
        )
        document = IgnoresDocument.for_config(ConfigDocument.load(config_path))
        with pytest.raises(GuiError):
            document.save()
        document.add_rule("r")
        document.save(tmp_path / "out.yaml")
        assert (tmp_path / "out.yaml").exists()


DSN = "postgresql://u:s3cret@h/db"
PAIRS = "host=h password=s3cret"


_LISTS = ("names", "targets", "attributes", "kinds", "statuses")


class TestPastedConnectionStrings:
    """The ignores file is committed and hand-editable, like the config."""

    @pytest.mark.parametrize("secret", [DSN, PAIRS])
    @pytest.mark.parametrize(
        ("field", "value", "error_field"),
        [
            ("names", None, "rules.1.names.0"),
            ("targets", None, "rules.1.targets.0"),
            ("attributes", None, "rules.1.attributes.0"),
            ("kinds", None, "rules.1.kinds.0"),
            ("statuses", None, "rules.1.statuses.0"),
            ("action", None, "rules.1.action"),
            ("reason", None, "rules.1.reason"),
            ("id", None, "rules.1.id"),
        ],
    )
    def test_refused_everywhere_by_position(self, document, field, value, error_field, secret):
        before = document.path.read_text()
        document.update_rule("dev-experiments", field, [secret] if field in _LISTS else secret)
        errors = document.validate()
        assert error_field in [e.field for e in errors]
        assert all("s3cret" not in str(e) and "s3cret" not in (e.field or "") for e in errors)
        assert any("rule 2" in str(e) for e in errors)
        with pytest.raises(GuiError) as exc:
            document.to_yaml()
        assert "s3cret" not in str(exc.value)
        with pytest.raises(GuiError) as exc:
            document.save()
        assert "s3cret" not in str(exc.value)
        assert document.path.read_text() == before

    def test_a_dsn_shaped_id_is_refused_before_the_duplicate_check_can_echo_it(self, document):
        # Make it a duplicate as well: were the duplicate check first, it would echo the id.
        document.update_rule("audit-archive", "id", DSN)
        with pytest.raises(GuiError) as exc:
            document.add_rule(DSN)
        assert "s3cret" not in str(exc.value)
        assert exc.value.field == "id"
        assert "does not belong" in str(exc.value)

    def test_a_dsn_shaped_new_rule_is_never_added(self):
        doc = IgnoresDocument.for_config(ConfigDocument.blank("x"))
        with pytest.raises(GuiError):
            doc.add_rule(DSN)
        assert doc.config.rules == ()
        assert doc.dirty is False
