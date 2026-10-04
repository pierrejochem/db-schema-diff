"""Editing an ignore ruleset.

The pick-lists matter as much as the editing: an ignore rule with an invalid value is refused by the
validator, and a form that can produce one wastes the user's time.
"""

from __future__ import annotations

import textwrap

import pytest

from db_schema_comparer.config.loader import load_config_files
from db_schema_comparer.gui.config_vm import ConfigDocument
from db_schema_comparer.gui.errors import GuiError
from db_schema_comparer.gui.ignores_vm import (
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


def read_config(path) -> ConfigDocument:
    """A configuration document from a file, through the library's loader.

    `ConfigDocument` does not read files any more; the application never opens one.
    """
    from pathlib import Path

    ((_, config),) = load_config_files([Path(path)])
    return ConfigDocument(path=Path(path), config=config)


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
    return IgnoresDocument.for_config(read_config(config_path))


class TestPickLists:
    def test_statuses_offer_only_what_the_validator_accepts(self):
        """ObjectStatus has four members; IgnoreRule accepts three.

        Building the list from ObjectStatus would offer `match`, which the validator rejects — a
        value the form should not be able to produce.
        """
        assert RULE_STATUSES == ("missing_in_target", "extra_in_target", "differs")
        assert "match" not in RULE_STATUSES

    def test_kinds_come_from_the_object_kinds(self):
        from db_schema_comparer.model.kinds import ObjectKind

        assert set(rule_kinds()) == {kind.value for kind in ObjectKind}

    def test_actions_are_the_three_the_model_allows(self):
        assert RULE_ACTIONS == ("ignore", "warn", "info")

    @pytest.mark.parametrize("status", RULE_STATUSES)
    def test_every_offered_status_validates(self, status):
        from db_schema_comparer.config.model import IgnoreRule

        IgnoreRule.model_validate({"id": "r", "statuses": [status]})

    @pytest.mark.parametrize("kind", rule_kinds())
    def test_every_offered_kind_validates(self, kind):
        from db_schema_comparer.config.model import IgnoreRule

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
        # The seed satisfies the model, but it is a placeholder and validation says so.
        document.add_rule("new-rule")
        errors = document.validate()
        assert [e.field for e in errors] == ["rules.2.names"]
        assert "cannot match yet" in str(errors[0])
        document.update_rule("new-rule", "names", ["public.orders"])
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
            IgnoresDocument.for_config(read_config(config_path))
        assert "not both" in str(exc.value)
        assert exc.value.field == "ignores"

    def test_neither_declared_starts_from_an_empty_ruleset(self, tmp_path):
        config_path = tmp_path / "invoicing.yaml"
        config_path.write_text(
            "version: 1\nname: invoicing\n"
            "master:\n  label: prod\n  dsn_env: P\n"
            "targets:\n- label: qa\n  dsn_env: Q\n"
        )
        document = IgnoresDocument.for_config(read_config(config_path))
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
        reloaded = IgnoresDocument.for_config(read_config(document.path.parent / "invoicing.yaml"))
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
        document = IgnoresDocument.for_config(read_config(config_path))
        with pytest.raises(GuiError):
            document.save()
        document.add_rule("r")
        document.update_rule("r", "names", ["public.*"])
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


def _fields(document):
    return [e.field for e in document.validate()]


class TestEachUnmatchableBranch:
    """One test per branch, each failing if that branch is removed."""

    def test_attribute_of_an_unknown_kind(self, document):
        document.update_rule("audit-archive", "attributes", ["widget.foo"])
        assert _fields(document) == ["rules.0.attributes"]
        assert "kind that does not exist" in str(document.validate()[0])

    @pytest.mark.parametrize("attribute", ["collation", "column.a.b", "column."])
    def test_malformed_attribute(self, document, attribute):
        document.update_rule("audit-archive", "attributes", [attribute])
        assert _fields(document) == ["rules.0.attributes"]
        assert "kind.attribute" in str(document.validate()[0])

    def test_attribute_never_compared(self, document):
        document.update_rule("audit-archive", "attributes", ["column.nonexistent"])
        assert _fields(document) == ["rules.0.attributes"]
        assert "never compared" in str(document.validate()[0])

    def test_attribute_on_a_kind_with_no_specs(self, document):
        document.update_rule("audit-archive", "attributes", ["schema.foo"])
        assert "never compared" in str(document.validate()[0])

    def test_a_real_attribute_is_accepted(self, document):
        document.update_rule("audit-archive", "attributes", ["column.collation", "index.name"])
        assert document.validate() == []

    def test_attribute_kind_excluded_by_kinds(self, document):
        document.update_rule("dev-experiments", "attributes", ["column.collation"])
        document.update_rule("dev-experiments", "statuses", ["differs"])
        errors = document.validate()
        assert [e.field for e in errors] == ["rules.1.attributes"]
        assert "exclude" in str(errors[0])

    def test_attribute_rule_excluding_differs(self, document):
        document.update_rule("audit-archive", "attributes", ["column.collation"])
        document.update_rule("audit-archive", "statuses", ["missing_in_target"])
        errors = document.validate()
        assert [e.field for e in errors] == ["rules.0.statuses"]

    @pytest.mark.parametrize("bad", [["audit_archive"], ["a", "public.x"]])
    def test_name_without_dot_or_wildcard(self, document, bad):
        document.update_rule("audit-archive", "names", bad)
        assert _fields(document) == ["rules.0.names"]
        assert "no dot and no wildcard" in str(document.validate()[0])

    @pytest.mark.parametrize("ok", ["public.orders", "*", "*.qrtz_*", "audit?", "[a]b"])
    def test_name_forms_that_can_match(self, document, ok):
        document.update_rule("audit-archive", "names", [ok])
        assert document.validate() == []

    def test_a_comma_inside_a_character_class_is_reported_not_swallowed(self, document):
        # The comma-separated field splits "x.[a,b]" into "x.[a" and "b]".
        document.update_rule("audit-archive", "names", "x.[a,b]")
        assert document.config.rules[0].names == ("x.[a", "b]")
        errors = [e for e in document.validate() if "unbalanced" in str(e)]
        assert [e.field for e in errors] == ["rules.0.names"]
        assert "x.[a" not in str(errors[0])  # reported by position, no value echoed

    @pytest.mark.parametrize("ok", ["x.[ab]", "public.*"])
    def test_balanced_globs_raise_no_bracket_error(self, document, ok):
        document.update_rule("audit-archive", "names", ok)
        assert not [e for e in document.validate() if "unbalanced" in str(e)]

    def test_empty_name_from_a_loaded_file(self, document):
        rule = document.config.rules[0].model_copy(update={"names": ("",)})
        document.config = document.config.model_copy(update={"rules": (rule,)})
        errors = document.validate()
        assert any("empty pattern" in str(e) for e in errors)

    def test_empty_target_from_a_loaded_file(self, document):
        rule = document.config.rules[1].model_copy(update={"targets": ("",)})
        document.config = document.config.model_copy(update={"rules": (rule,)})
        assert _fields(document) == ["rules.0.targets"]
        assert "empty target" in str(document.validate()[0])

    def test_action_outside_the_three(self, document):
        rule = document.config.rules[0].model_copy(update={"action": "silence"})
        document.config = document.config.model_copy(update={"rules": (rule,)})
        errors = document.validate()
        assert [e.field for e in errors] == ["rules.0.action"]
        assert "ignore, warn or info" in str(errors[0])
        assert "silence" not in str(errors[0])

    def test_unknown_kind_and_status_are_each_reported_on_their_own_field(self, document):
        document.update_rule("dev-experiments", "kinds", ["tabel"])
        assert _fields(document) == ["rules.1.kinds"]
        document.update_rule("dev-experiments", "kinds", ["index"])
        document.update_rule("dev-experiments", "statuses", ["match"])
        assert _fields(document) == ["rules.1.statuses"]

    def test_duplicate_id_says_the_file_is_invalid(self, document):
        document.update_rule("dev-experiments", "id", "audit-archive")
        assert "invalid" in str(document.validate()[0])
        assert "never be reached" not in str(document.validate()[0])

    def test_a_rule_replacing_a_bundled_default_is_reported(self, document):
        document.add_rule("scratch-objects")
        document.update_rule("scratch-objects", "names", ["public.tmp_*"])
        errors = document.validate()
        assert [e.field for e in errors] == ["rules.2.id"]
        assert "bundled default" in str(errors[0])


class TestLoading:
    def test_whitespace_in_a_loaded_file_is_stripped(self, tmp_path):
        (tmp_path / "ignores.yaml").write_text(
            "version: 1\nrules:\n- id: r\n  names:\n  - 'a.* '\n  targets:\n  - ' qa'\n"
        )
        config_path = tmp_path / "c.yaml"
        config_path.write_text(
            "version: 1\nname: c\nmaster:\n  label: p\n  dsn_env: P\n"
            "targets:\n- label: qa\n  dsn_env: Q\nignores_file: ignores.yaml\n"
        )
        document = IgnoresDocument.for_config(read_config(config_path))
        assert document.config.rules[0].names == ("a.*",)
        assert document.config.rules[0].targets == ("qa",)
        assert document.dirty is False

    def test_a_missing_ignores_file_starts_empty_at_that_path(self, tmp_path):
        config_path = tmp_path / "c.yaml"
        config_path.write_text(
            "version: 1\nname: c\nmaster:\n  label: p\n  dsn_env: P\n"
            "targets:\n- label: qa\n  dsn_env: Q\nignores_file: later.yaml\n"
        )
        document = IgnoresDocument.for_config(read_config(config_path))
        assert document.path == tmp_path / "later.yaml"
        assert document.config.rules == ()
        document.add_rule("r")
        document.update_rule("r", "names", ["public.*"])
        document.save()
        assert (tmp_path / "later.yaml").exists()


class TestShapeIsCredentialsOnly:
    @pytest.mark.parametrize(
        "text",
        [
            "postgresql://u:s3cret@h/db",
            "host=h password=s3cret",
            "postgresql://h/db",
            "postgres://x",
            "POSTGRESQL://h",
            "https://u:p@example.com/x",
            "sslkey=/k",
            "passfile = /p",
            "sslpassword=x",
        ],
    )
    def test_refused(self, text):
        from db_schema_comparer.gui.shape import looks_like_connection_string

        assert looks_like_connection_string(text)

    @pytest.mark.parametrize(
        "text",
        [
            "see https://jira.example.com/X-1",
            "ops set user = readonly here",
            "service=batch creates it",
            "http://docs.example.com",
            "see https://x.com/a?b=c#d and mail a@b.c",
            "host=h dbname=d port=5432 sslmode=require",
        ],
    )
    def test_accepted(self, text):
        from db_schema_comparer.gui.shape import looks_like_connection_string

        assert not looks_like_connection_string(text)

    def test_a_url_in_a_reason_is_allowed(self, document):
        document.update_rule("audit-archive", "reason", "see https://jira.example.com/X-1")
        assert document.validate() == []
        document.save()
        assert "jira.example.com" in document.path.read_text()
