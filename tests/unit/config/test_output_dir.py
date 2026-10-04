"""``output_dir``: where reports go, remembered in the configuration.

The desktop application asked for an output directory on every run and forgot it on exit, and the
command line took one only as a flag. It is a property of a comparison — this service's reports go
here — so it belongs in the file that describes the comparison.

It follows ``ignores_file`` in every respect, because the same problem applies: a path in a file
that may be committed to git must not be one machine's home directory, so a relative path resolves
against the config that named it. :func:`resolve_output_dir` is where that happens, and it is the
only place allowed to decide it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from db_schema_comparer.config.loader import resolve_output_dir
from db_schema_comparer.config.model import ComparerConfig

BASE = {
    "version": 1,
    "name": "invoicing",
    "master": {"label": "prod", "dsn_env": "PROD_DSN"},
    "targets": [{"label": "qa", "dsn_env": "QA_DSN"}],
}


def config(**overrides) -> ComparerConfig:
    return ComparerConfig.model_validate({**BASE, **overrides})


class TestTheField:
    def test_it_is_absent_by_default(self):
        """A configuration that says nothing about reports must behave exactly as before."""
        assert config().output_dir is None

    def test_a_relative_path_is_kept_verbatim(self):
        assert config(output_dir="reports").output_dir == "reports"

    def test_an_absolute_path_is_kept_verbatim(self):
        assert config(output_dir="/srv/reports").output_dir == "/srv/reports"

    def test_it_round_trips_through_a_dump(self):
        dumped = config(output_dir="reports").model_dump(exclude_unset=True)
        assert dumped["output_dir"] == "reports"

    @pytest.mark.parametrize("value", ["", "   "])
    def test_an_empty_path_is_refused_rather_than_meaning_here(self, value):
        """An empty string would resolve to the config's own directory, which nobody typed."""
        with pytest.raises(ValidationError):
            config(output_dir=value)


class TestResolution:
    def test_a_relative_path_resolves_against_the_config(self):
        """So a checkout works wherever it is cloned, which is the whole point of relative."""
        resolved = resolve_output_dir(
            config(output_dir="reports"), Path("/repo/cfg/invoicing.yaml")
        )
        assert resolved == Path("/repo/cfg/reports")

    def test_an_absolute_path_is_left_where_it_points(self):
        resolved = resolve_output_dir(config(output_dir="/srv/out"), Path("/repo/cfg/x.yaml"))
        assert resolved == Path("/srv/out")

    def test_a_tilde_is_expanded(self):
        """Somebody will type one, and a directory literally named ``~`` is not what they meant."""
        resolved = resolve_output_dir(config(output_dir="~/reports"), Path("/repo/x.yaml"))
        assert resolved == Path.home() / "reports"

    def test_no_output_dir_resolves_to_nothing(self):
        assert resolve_output_dir(config(), Path("/repo/x.yaml")) is None

    def test_without_a_config_path_a_relative_path_is_relative_to_the_working_directory(self):
        """The in-memory case: a configuration that has never been saved has nothing to be
        relative to, and the working directory is the only other answer."""
        assert resolve_output_dir(config(output_dir="reports"), None) == Path("reports")
