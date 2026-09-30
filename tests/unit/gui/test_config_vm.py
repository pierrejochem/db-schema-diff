"""Editing a configuration without breaking the file it came from.

The application reads and writes the same YAML the CLI reads, and a file it saves must stay
hand-editable. The round-trip assertions are the ones that matter: everything else is a form.
"""

from __future__ import annotations

import pathlib
import textwrap

import pytest

from cumo_schema_comparer.gui.config_vm import ConfigDocument
from cumo_schema_comparer.gui.errors import GuiError

FULL = textwrap.dedent(
    """
    version: 1
    name: invoicing
    master:
      label: prod
      host: db-prod
      database: invoicing
      dsn_env: PROD_INVOICING_DSN
      schemas:
        - cumo-invoicing
        - public
    targets:
      - label: qa
        dsn_env: QA_INVOICING_DSN
        schema_map:
          cumo-invoicing: invoicing_qa
      - label: local
        dsn_env: LOCAL_DSN
        liquibase:
          schema: cumo-invoicing
          table: DATABASECHANGELOG
    exclude_schemas:
      - quartz
    options:
      fail_on: warning
      max_workers: 2
    """
).lstrip()


@pytest.fixture
def document(tmp_path) -> ConfigDocument:
    path = tmp_path / "invoicing.yaml"
    path.write_text(FULL)
    return ConfigDocument.load(path)


class TestRoundTrip:
    def test_loading_and_saving_an_untouched_file_changes_nothing(self, document, tmp_path):
        """The strongest guarantee this layer offers.

        Somebody opens a config to look at it, saves out of habit, and the diff must be empty.
        """
        out = tmp_path / "again.yaml"
        document.save(out)
        assert out.read_text() == FULL

    def test_the_shipped_example_round_trips_byte_identically(self, tmp_path):
        source = pathlib.Path("config.example.yaml")
        # The example carries comments, which YAML loading discards; compare the parsed result.
        loaded = ConfigDocument.load(source)
        out = tmp_path / "example.yaml"
        loaded.save(out)
        assert ConfigDocument.load(out).config == loaded.config

    def test_only_non_default_fields_are_written(self, tmp_path):
        document = ConfigDocument.blank("payment")
        out = tmp_path / "payment.yaml"
        document.save(out)
        text = out.read_text()
        # Defaults would bloat a hand-edited file with noise.
        assert "include_owners" not in text
        assert "connect_timeout_seconds" not in text
        assert "name: payment" in text

    def test_a_hyphenated_identifier_survives_the_round_trip(self, document, tmp_path):
        """Review Focus 5: `cumo-invoicing` is a real schema name in this platform.

        It passes through a Slint string and back into YAML, where it must stay correctly quoted or
        unquoted such that reloading produces the same value.
        """
        out = tmp_path / "again.yaml"
        document.save(out)
        reloaded = ConfigDocument.load(out)
        assert reloaded.config.master.schemas == ("cumo-invoicing", "public")
        assert reloaded.config.targets[0].schema_map == {"cumo-invoicing": "invoicing_qa"}
        assert reloaded.config.targets[1].liquibase.schema_name == "cumo-invoicing"

    def test_a_non_ascii_identifier_survives_the_round_trip(self, tmp_path):
        document = ConfigDocument.blank("faktura")
        document.update_source("prod", "database", "fakturierung_äöü")
        out = tmp_path / "x.yaml"
        document.save(out)
        assert ConfigDocument.load(out).config.master.database == "fakturierung_äöü"


class TestEditing:
    def test_source_rows_expose_every_source(self, document):
        rows = document.source_rows()
        assert [row["label"] for row in rows] == ["prod", "qa", "local"]
        assert rows[0]["is_master"] is True
        assert rows[1]["is_master"] is False
        assert rows[0]["dsn_env"] == "PROD_INVOICING_DSN"

    def test_updating_a_field_marks_the_document_dirty(self, document):
        assert document.dirty is False
        document.update_source("qa", "host", "db-qa-2")
        assert document.dirty is True
        assert document.config.targets[0].host == "db-qa-2"

    def test_adding_and_removing_a_target(self, document):
        document.add_target("dev")
        assert [t.label for t in document.config.targets] == ["qa", "local", "dev"]
        document.remove_target("qa")
        assert [t.label for t in document.config.targets] == ["local", "dev"]

    def test_a_duplicate_label_is_refused(self, document):
        with pytest.raises(GuiError) as exc:
            document.add_target("qa")
        assert "qa" in str(exc.value)

    def test_the_master_cannot_be_removed(self, document):
        with pytest.raises(GuiError, match="master"):
            document.remove_target("prod")


class TestValidation:
    def test_a_valid_document_reports_no_errors(self, document):
        assert document.validate() == []

    def test_a_validation_error_carries_the_field_path(self, document):
        document.update_source("qa", "dsn_env", "")
        errors = document.validate()
        assert errors
        assert any(error.field and "dsn_env" in error.field for error in errors)

    def test_every_error_is_a_gui_error_not_a_traceback(self, document):
        document.update_source("qa", "dsn_env", "")
        errors = document.validate()
        assert errors
        assert all(isinstance(error, GuiError) for error in errors)

    def test_loading_an_invalid_file_raises_a_gui_error(self, tmp_path):
        path = tmp_path / "broken.yaml"
        path.write_text(FULL.replace("dsn_env: QA_INVOICING_DSN", "dsn_ev: QA_INVOICING_DSN"))
        with pytest.raises(GuiError) as exc:
            ConfigDocument.load(path)
        assert "dsn_ev" in str(exc.value)


class TestSaveFailures:
    """Review Focus 3: saving can fail, and must not produce a traceback."""

    def test_saving_to_an_unwritable_directory_is_a_gui_error(self, document, tmp_path):
        readonly = tmp_path / "readonly"
        readonly.mkdir()
        readonly.chmod(0o500)
        try:
            with pytest.raises(GuiError) as exc:
                document.save(readonly / "out.yaml")
            assert "out.yaml" in str(exc.value)
        finally:
            readonly.chmod(0o700)

    def test_saving_to_a_directory_that_does_not_exist_is_a_gui_error(self, document, tmp_path):
        with pytest.raises(GuiError):
            document.save(tmp_path / "absent" / "nested" / "out.yaml")

    def test_a_failed_save_leaves_the_document_dirty(self, document, tmp_path):
        document.update_source("qa", "host", "db-qa-2")
        with pytest.raises(GuiError):
            document.save(tmp_path / "absent" / "out.yaml")
        assert document.dirty is True


class TestComments:
    def test_a_file_with_comments_is_flagged_once(self, tmp_path):
        """Loading discards comments; saving would drop them silently.

        The application warns rather than quietly rewriting somebody's annotated file.
        """
        path = tmp_path / "commented.yaml"
        path.write_text("# why this exists\n" + FULL)
        assert ConfigDocument.load(path).had_comments() is True

    def test_a_file_without_comments_is_not_flagged(self, document):
        assert document.had_comments() is False

    def test_a_hash_inside_a_value_is_not_a_comment(self, tmp_path):
        path = tmp_path / "hash.yaml"
        path.write_text(FULL.replace("name: invoicing", "name: invoicing#1"))
        assert ConfigDocument.load(path).had_comments() is False

    def test_an_indented_comment_is_flagged(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text(FULL.replace("  host: db-prod", "  # primary\n  host: db-prod"))
        assert ConfigDocument.load(path).had_comments() is True


SECRET = "postgresql://u:s3cret@h/db"


class TestCredentialsNeverReachTheFile:
    def test_a_dsn_in_dsn_env_is_rejected_without_echoing_it(self, document):
        document.update_source("qa", "dsn_env", SECRET)
        errors = document.validate()
        assert errors
        assert any(error.field == "targets.0.dsn_env" for error in errors)
        for error in errors:
            assert "s3cret" not in str(error)
            assert SECRET not in str(error)

    def test_to_yaml_and_save_refuse_it(self, document, tmp_path):
        document.update_source("qa", "dsn_env", SECRET)
        with pytest.raises(GuiError) as exc:
            document.to_yaml()
        assert "s3cret" not in str(exc.value)
        out = tmp_path / "out.yaml"
        with pytest.raises(GuiError) as exc:
            document.save(out)
        assert "s3cret" not in str(exc.value)
        assert not out.exists()

    def test_the_master_is_checked_too(self, document):
        document.update_source("prod", "dsn_env", "a b")
        assert any(error.field == "master.dsn_env" for error in document.validate())

    def test_surrounding_whitespace_is_stripped(self, document):
        document.update_source("qa", "dsn_env", "  QA_DSN  ")
        assert document.config.targets[0].dsn_env == "QA_DSN"
        assert document.validate() == []
        assert "dsn_env: QA_DSN\n" in document.to_yaml()


class TestFurtherEditing:
    def test_edits_survive_save_and_reload(self, document, tmp_path):
        document.update_source("prod", "host", "db-prod-2")
        document.update_source("prod", "schemas", ["a-b", "c"])
        document.add_target("dev")
        out = tmp_path / "e.yaml"
        document.save(out)
        again = ConfigDocument.load(out).config
        assert again.master.host == "db-prod-2"
        assert again.master.schemas == ("a-b", "c")
        assert [t.label for t in again.targets] == ["qa", "local", "dev"]
        assert again.targets[2].dsn_env == "DEV_DSN"

    def test_a_comma_separated_string_becomes_schema_names(self, document):
        document.update_source("prod", "schemas", "a, b-c ,")
        assert document.config.master.schemas == ("a", "b-c")

    def test_an_unknown_field_is_refused(self, document):
        with pytest.raises(GuiError, match="hots"):
            document.update_source("qa", "hots", "x")

    def test_saving_clears_dirty_and_adopts_the_path(self, document, tmp_path):
        document.update_source("qa", "host", "h")
        out = tmp_path / "new.yaml"
        document.save(out)
        assert document.dirty is False
        assert document.path == out

    def test_loading_a_directory_of_configs_is_a_gui_error(self, tmp_path):
        (tmp_path / "a.yaml").write_text(FULL)
        (tmp_path / "b.yaml").write_text(FULL)
        with pytest.raises(GuiError, match="single"):
            ConfigDocument.load(tmp_path)

    def test_sequences_are_indented_under_their_key(self, document):
        assert "  schemas:\n    - cumo-invoicing\n" in document.to_yaml()

    def test_ignores_file_is_rewritten_on_save_as_elsewhere(self, tmp_path):
        first = tmp_path / "a"
        second = tmp_path / "b" / "deeper"
        first.mkdir()
        second.mkdir(parents=True)
        (first / "ignores.yaml").write_text("version: 1\n")
        (first / "c.yaml").write_text(FULL + "ignores_file: ignores.yaml\n")
        document = ConfigDocument.load(first / "c.yaml")
        out = second / "c.yaml"
        document.save(out)
        reloaded = ConfigDocument.load(out)
        assert reloaded.config.ignores_file is not None
        assert (second / reloaded.config.ignores_file).resolve() == (
            first / "ignores.yaml"
        ).resolve()
