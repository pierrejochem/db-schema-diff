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
        assert all(isinstance(error, GuiError) for error in document.validate())

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
