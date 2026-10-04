"""Editing a configuration without breaking the file it came from.

The application reads and writes the same YAML the CLI reads, and a file it saves must stay
hand-editable. The round-trip assertions are the ones that matter: everything else is a form.
"""

from __future__ import annotations

import pathlib
import textwrap

import pytest

from db_schema_diff.config.loader import load_config_files
from db_schema_diff.gui.config_vm import ConfigDocument
from db_schema_diff.gui.errors import GuiError

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
        - acme-invoicing
        - public
    targets:
      - label: qa
        dsn_env: QA_INVOICING_DSN
        schema_map:
          acme-invoicing: invoicing_qa
      - label: local
        dsn_env: LOCAL_DSN
        liquibase:
          schema: acme-invoicing
          table: DATABASECHANGELOG
    exclude_schemas:
      - quartz
    options:
      fail_on: warning
      max_workers: 2
    """
).lstrip()


def read(path) -> ConfigDocument:
    """A document from a file, for tests that need one to start from.

    `ConfigDocument` does not read files any more — the application never opens one — so this uses
    the library's own loader, which the command-line tool uses and which is not part of the GUI.
    """
    ((_, config),) = load_config_files([pathlib.Path(path)])
    return ConfigDocument(path=pathlib.Path(path), config=config)


@pytest.fixture
def document(tmp_path) -> ConfigDocument:
    path = tmp_path / "invoicing.yaml"
    path.write_text(FULL)
    return read(path)


class TestRoundTrip:
    def test_the_shipped_example_round_trips_byte_identically(self, tmp_path):
        source = pathlib.Path("config.example.yaml")
        # The example carries comments, which YAML loading discards; compare the parsed result.
        loaded = read(source)
        out = tmp_path / "example.yaml"
        loaded.save(out)
        assert read(out).config == loaded.config

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
        """Review Focus 5: `acme-invoicing` is a real schema name in this platform.

        It passes through a Slint string and back into YAML, where it must stay correctly quoted or
        unquoted such that reloading produces the same value.
        """
        out = tmp_path / "again.yaml"
        document.save(out)
        reloaded = read(out)
        assert reloaded.config.master.schemas == ("acme-invoicing", "public")
        assert reloaded.config.targets[0].schema_map == {"acme-invoicing": "invoicing_qa"}
        assert reloaded.config.targets[1].liquibase.schema_name == "acme-invoicing"

    def test_a_non_ascii_identifier_survives_the_round_trip(self, tmp_path):
        document = ConfigDocument.blank("faktura")
        document.update_source("prod", "database", "fakturierung_äöü")
        out = tmp_path / "x.yaml"
        document.save(out)
        assert read(out).config.master.database == "fakturierung_äöü"


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
        again = read(out).config
        assert again.master.host == "db-prod-2"
        assert again.master.schemas == ("a-b", "c")
        assert [t.label for t in again.targets] == ["qa", "local", "dev"]
        # Generated from the label rather than typed: nobody is asked to invent a variable name.
        assert again.targets[2].dsn_env == "DB_DEV_DSN"

    def test_two_labels_that_differ_only_in_punctuation_get_separate_variables(self, document):
        """Punctuation collapses to underscores, so these two generate the same name.

        Filed under one variable they would share one keychain entry — one password for two
        databases, and the configuration would only *warn* that a target reads the master's
        credential.
        """
        document.add_target("db.qa")
        document.add_target("db-qa")
        names = [t.dsn_env for t in document.config.targets[-2:]]
        assert names == ["DB_DB_QA_DSN", "DB_DB_QA_2_DSN"]

    def test_a_rename_onto_a_colliding_name_is_separated_too(self, document):
        document.add_target("db.qa")
        document.add_target("other")
        document.update_source("other", "label", "db-qa")
        envs = {t.label: t.dsn_env for t in document.config.targets}
        assert envs["db.qa"] != envs["db-qa"]

    def test_a_rename_may_reuse_the_name_the_source_itself_held(self, document):
        """Its own entry is not a collision: ``qa`` to ``qa`` and back must not drift to ``_2``."""
        document.add_target("dev")
        document.update_source("dev", "label", "dev-x")
        document.update_source("dev-x", "label", "dev")
        assert document.config.targets[-1].dsn_env == "DB_DEV_DSN"

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

    def test_sequences_are_indented_under_their_key(self, document):
        assert "  schemas:\n    - acme-invoicing\n" in document.to_yaml()

    def test_a_sources_connection_is_written_in_the_order_it_is_asked_for(self, document):
        """A saved file has to stay hand-editable, and that includes reading sensibly.

        Before the connection fields were added to the ordering list, ``port`` was written after
        ``dsn_env`` while ``host`` sat before it — the fields of one connection split either side
        of an unrelated key.
        """
        for field, value in (
            ("host", "db-prod"),
            ("port", 5432),
            ("user", "app"),
            ("sslmode", "require"),
        ):
            document.update_source("prod", field, value)
        rendered = document.to_yaml()
        written = [
            line.strip().split(":")[0]
            for line in rendered.splitlines()[rendered.splitlines().index("master:") + 1 :]
            if line.startswith("  ") and not line.startswith("    ")
        ]
        connection = [f for f in written if f in {"host", "port", "database", "user", "sslmode"}]
        assert connection == ["host", "port", "database", "user", "sslmode"]
        assert written.index("sslmode") < written.index("dsn_env")

    def test_ignores_file_is_rewritten_on_save_as_elsewhere(self, tmp_path):
        first = tmp_path / "a"
        second = tmp_path / "b" / "deeper"
        first.mkdir()
        second.mkdir(parents=True)
        (first / "ignores.yaml").write_text("version: 1\n")
        (first / "c.yaml").write_text(FULL + "ignores_file: ignores.yaml\n")
        document = read(first / "c.yaml")
        out = second / "c.yaml"
        document.save(out)
        reloaded = read(out)
        assert reloaded.config.ignores_file is not None
        assert (second / reloaded.config.ignores_file).resolve() == (
            first / "ignores.yaml"
        ).resolve()


class TestPastedConnectionStrings:
    @pytest.mark.parametrize(
        ("edit", "expected_field"),
        [
            (lambda d: d.add_target(SECRET), "targets.2.label"),
            (lambda d: d.update_source("qa", "host", SECRET), "targets.0.host"),
            (lambda d: d.update_source("qa", "database", SECRET), "targets.0.database"),
            (lambda d: d.update_source("prod", "schemas", ["ok", SECRET]), "master.schemas.1"),
            (
                lambda d: d.update_source("qa", "host", "host=h password=s3cret"),
                "targets.0.host",
            ),
            (
                lambda d: d.update_source("qa", "schema_map", {"a": SECRET}),
                "targets.0.schema_map",
            ),
            (
                lambda d: d.update_source("qa", "schema_map", {SECRET: "a"}),
                "targets.0.schema_map",
            ),
        ],
    )
    def test_refused_by_shape_without_echoing(self, document, tmp_path, edit, expected_field):
        edit(document)
        errors = document.validate()
        assert [e.field for e in errors] == [expected_field]
        assert "does not belong" in str(errors[0])
        for text in (str(errors[0]),):
            assert "s3cret" not in text
            assert "postgresql" not in text
        out = tmp_path / "o.yaml"
        for action in (document.to_yaml, lambda: document.save(out)):
            with pytest.raises(GuiError) as exc:
                action()
            assert "s3cret" not in str(exc.value)
        assert not out.exists()

    def test_ordinary_values_are_not_mistaken_for_connection_strings(self, document):
        document.update_source("qa", "host", "db-qa.internal")
        document.update_source("qa", "database", "user_data")
        assert document.validate() == []

    def test_a_trailing_newline_does_not_make_an_env_name_valid(self, document):
        document.config = document.config.model_copy(
            update={"targets": (document.config.targets[0].model_copy(update={"dsn_env": "X\n"}),)}
        )
        assert any(e.field == "targets.0.dsn_env" for e in document.validate())

    def test_save_as_adopts_the_rewritten_ignores_file_and_is_idempotent(self, tmp_path):
        first = tmp_path / "a"
        second = tmp_path / "b" / "deeper"
        first.mkdir()
        second.mkdir(parents=True)
        (first / "c.yaml").write_text(FULL + "ignores_file: ignores.yaml\n")
        document = read(first / "c.yaml")
        out = second / "c.yaml"
        document.save(out)
        written = out.read_text()
        assert "ignores_file: ../../a/ignores.yaml\n" in written
        assert document.config.ignores_file == "../../a/ignores.yaml"
        document.save(out)
        assert out.read_text() == written
        document.save()
        assert out.read_text() == written
