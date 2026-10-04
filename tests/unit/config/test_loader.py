"""Config loading and validation.

This YAML is hand-edited and is the most likely thing to be wrong, so the messages matter as
much as the parsing.
"""

import pytest

from db_schema_comparer.config.loader import load_config
from db_schema_comparer.errors import ConfigError, MissingCredentialsError

MINIMAL = """
version: 1
name: invoicing
master:
  label: prod
  dsn_env: PROD_DSN
targets:
  - label: qa
    dsn_env: QA_DSN
"""

FULL = """
version: 1
name: invoicing
master:
  label: prod
  host: db-prod
  database: invoicing
  dsn_env: PROD_DSN
  schemas: ["acme-invoicing", "public"]
targets:
  - label: qa
    host: db-qa
    dsn_env: QA_DSN
    schema_map: { "acme-invoicing": "invoicing_qa" }
  - label: local
    dsn_env: LOCAL_DSN
    liquibase: { schema: "acme-invoicing", table: DATABASECHANGELOG }
exclude_schemas: ["quartz"]
options:
  fail_on: warning
  max_workers: 2
  parallel: false
"""


def write(tmp_path, text, name="config.yaml"):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_minimal_config_loads_with_documented_defaults(tmp_path):
    cfg = load_config([write(tmp_path, MINIMAL)])[0]
    assert cfg.name == "invoicing"
    assert cfg.master.label == "prod"
    assert [t.label for t in cfg.targets] == ["qa"]
    # Defaults: every non-system schema, no ignores file, error-level gate, parallel capture.
    assert cfg.master.schemas is None
    assert cfg.exclude_schemas == ()
    assert cfg.options.fail_on == "error"
    assert cfg.options.parallel is True


def test_full_config_round_trips_every_field(tmp_path):
    cfg = load_config([write(tmp_path, FULL)])[0]
    assert cfg.master.schemas == ("acme-invoicing", "public")
    qa, local = cfg.targets
    assert qa.schema_map == {"acme-invoicing": "invoicing_qa"}
    assert local.liquibase is not None
    assert local.liquibase.schema_name == "acme-invoicing"
    assert local.liquibase.table == "DATABASECHANGELOG"
    assert cfg.exclude_schemas == ("quartz",)
    assert cfg.options.fail_on == "warning"
    assert cfg.options.max_workers == 2


def test_hyphenated_schema_names_survive_verbatim(tmp_path):
    # acme-invoicing really does use a quoted, hyphenated schema. Never normalise a name.
    cfg = load_config([write(tmp_path, FULL)])[0]
    assert "acme-invoicing" in cfg.master.schemas


def test_a_typo_in_a_key_is_an_error_not_a_silent_default(tmp_path):
    text = MINIMAL.replace("exclude_schemas", "exclude_schema").replace(
        "targets:", "exclude_schema: [x]\ntargets:"
    )
    with pytest.raises(ConfigError) as exc:
        load_config([write(tmp_path, text)])
    assert "exclude_schema" in str(exc.value)


def test_missing_required_field_names_its_path(tmp_path):
    text = MINIMAL.replace("    dsn_env: QA_DSN", "")
    with pytest.raises(ConfigError) as exc:
        load_config([write(tmp_path, text)])
    message = str(exc.value)
    assert "targets" in message and "dsn_env" in message


def test_duplicate_target_labels_are_rejected(tmp_path):
    text = MINIMAL + "  - label: qa\n    dsn_env: OTHER_DSN\n"
    with pytest.raises(ConfigError, match="duplicate"):
        load_config([write(tmp_path, text)])


def test_a_target_label_equal_to_the_master_label_is_rejected(tmp_path):
    text = MINIMAL.replace("  - label: qa", "  - label: prod")
    with pytest.raises(ConfigError, match="prod"):
        load_config([write(tmp_path, text)])


def test_at_least_one_target_is_required(tmp_path):
    text = MINIMAL.split("targets:")[0] + "targets: []\n"
    with pytest.raises(ConfigError, match="target"):
        load_config([write(tmp_path, text)])


def test_unknown_version_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="version"):
        load_config([write(tmp_path, MINIMAL.replace("version: 1", "version: 2"))])


def test_unknown_fail_on_value_is_rejected(tmp_path):
    text = MINIMAL + "options:\n  fail_on: sometimes\n"
    with pytest.raises(ConfigError, match="fail_on"):
        load_config([write(tmp_path, text)])


def test_malformed_yaml_reports_the_file(tmp_path):
    path = write(tmp_path, "version: 1\n  bad indent: [\n")
    with pytest.raises(ConfigError) as exc:
        load_config([path])
    assert path.name in str(exc.value)


def test_a_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match=r"nope\.yaml"):
        load_config([tmp_path / "nope.yaml"])


def test_an_empty_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config([write(tmp_path, "\n")])


def test_a_directory_loads_every_yaml_inside_it_in_a_stable_order(tmp_path):
    d = tmp_path / "configs"
    d.mkdir()
    write(d, MINIMAL.replace("invoicing", "payment"), "b-payment.yaml")
    write(d, MINIMAL, "a-invoicing.yaml")
    (d / "notes.txt").write_text("ignored")
    names = [c.name for c in load_config([d])]
    assert names == ["invoicing", "payment"]


def test_an_empty_directory_is_a_config_error(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    with pytest.raises(ConfigError, match="no config"):
        load_config([d])


def test_credentials_resolve_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PROD_DSN", "postgresql://u:p@prod/invoicing")
    monkeypatch.setenv("QA_DSN", "postgresql://u:p@qa/invoicing")
    cfg = load_config([write(tmp_path, MINIMAL)])[0]
    creds = cfg.resolve_credentials()
    assert creds["prod"].env_name == "PROD_DSN"
    assert creds["qa"].env_name == "QA_DSN"


def test_every_missing_credential_is_reported_in_one_failure(tmp_path, monkeypatch):
    monkeypatch.delenv("PROD_DSN", raising=False)
    monkeypatch.delenv("QA_DSN", raising=False)
    cfg = load_config([write(tmp_path, MINIMAL)])[0]
    with pytest.raises(MissingCredentialsError) as exc:
        cfg.resolve_credentials()
    message = str(exc.value)
    assert "PROD_DSN" in message and "QA_DSN" in message


def test_a_target_sharing_the_masters_dsn_var_warns_but_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("PROD_DSN", "postgresql://u:p@prod/invoicing")
    text = MINIMAL.replace("    dsn_env: QA_DSN", "    dsn_env: PROD_DSN")
    cfg = load_config([write(tmp_path, text)])[0]
    assert any("PROD_DSN" in w for w in cfg.warnings())
    assert cfg.resolve_credentials()["qa"].env_name == "PROD_DSN"


class TestIgnoresResolution:
    """Where a project's ruleset comes from."""

    def test_an_ignores_file_resolves_relative_to_its_config(self, tmp_path):
        # So a config directory can be checked out anywhere without rewriting paths inside it.
        from db_schema_comparer.config.loader import load_config_files, resolve_ignores

        (tmp_path / "ignores.yaml").write_text(
            "version: 1\nrules:\n  - id: mine\n    names: ['public.x']\n"
        )
        config_path = write(tmp_path, MINIMAL + "ignores_file: ignores.yaml\n")
        path, config = load_config_files([config_path])[0]
        resolved = resolve_ignores(config, path)
        assert resolved is not None
        assert [r.id for r in resolved.rules] == ["mine"]

    def test_an_inline_block_is_used_directly(self, tmp_path):
        from db_schema_comparer.config.loader import resolve_ignores

        text = (
            MINIMAL + "ignores:\n  version: 1\n  rules:\n    - id: inline\n      names: ['a.b']\n"
        )
        config = load_config([write(tmp_path, text)])[0]
        resolved = resolve_ignores(config, None)
        assert resolved is not None
        assert [r.id for r in resolved.rules] == ["inline"]

    def test_declaring_both_is_an_error(self, tmp_path):
        # Two sources for the same rules leaves it unclear which is in force.
        from db_schema_comparer.config.loader import resolve_ignores

        text = (
            MINIMAL
            + "ignores_file: ignores.yaml\n"
            + "ignores:\n  version: 1\n  rules:\n    - id: inline\n      names: ['a.b']\n"
        )
        config = load_config([write(tmp_path, text)])[0]
        with pytest.raises(ConfigError, match="not both"):
            resolve_ignores(config, None)

    def test_no_ignores_declared_returns_none(self, tmp_path):
        from db_schema_comparer.config.loader import resolve_ignores

        config = load_config([write(tmp_path, MINIMAL)])[0]
        assert resolve_ignores(config, None) is None

    def test_a_missing_ignores_file_is_a_config_error(self, tmp_path):
        from db_schema_comparer.config.loader import load_config_files, resolve_ignores

        config_path = write(tmp_path, MINIMAL + "ignores_file: absent.yaml\n")
        path, config = load_config_files([config_path])[0]
        with pytest.raises(ConfigError, match=r"absent\.yaml"):
            resolve_ignores(config, path)

    def test_load_config_files_reports_the_source_path(self, tmp_path):
        from db_schema_comparer.config.loader import load_config_files

        config_path = write(tmp_path, MINIMAL)
        [(path, config)] = load_config_files([config_path])
        assert path == config_path
        assert config.name == "invoicing"


def test_the_shipped_example_ruleset_is_valid():
    """The example is documentation people copy, so it must actually parse."""
    from pathlib import Path

    import yaml

    from db_schema_comparer.config.model import IgnoreConfig

    example = Path(__file__).parents[3] / "ignores.example.yaml"
    config = IgnoreConfig.model_validate(yaml.safe_load(example.read_text()))
    assert config.rules
    # Every example rule should model good practice by explaining itself.
    assert all(rule.reason for rule in config.rules)


def test_the_shipped_example_config_is_valid():
    from pathlib import Path

    import yaml

    from db_schema_comparer.config.model import ComparerConfig

    example = Path(__file__).parents[3] / "config.example.yaml"
    config = ComparerConfig.model_validate(yaml.safe_load(example.read_text()))
    assert [t.label for t in config.targets] == ["qa", "dev", "local"]
