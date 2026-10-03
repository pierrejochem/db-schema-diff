"""The folder the desktop application keeps configurations in.

The application does not read configurations back — no Open button, no path on the command line,
nothing reopened on start-up — so this folder is write-only from its point of view and nothing here
lists or chooses a file. What is left to defend is that it gets created, that a name typed by a
person cannot choose a different folder, and that none of this touches a real home directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cumo_schema_comparer.gui import home


class TestWhereItIs:
    def test_it_is_a_dotted_folder_in_the_home_directory(self, monkeypatch):
        monkeypatch.delenv(home.HOME_VARIABLE, raising=False)
        assert home.directory() == Path.home() / ".cumo_db_schema_comparer"
        assert home.DIRECTORY_NAME == ".cumo_db_schema_comparer"

    def test_the_override_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv(home.HOME_VARIABLE, str(tmp_path))
        assert home.directory() == tmp_path

    def test_an_override_with_a_tilde_is_expanded(self, monkeypatch):
        monkeypatch.setenv(home.HOME_VARIABLE, "~/elsewhere")
        assert home.directory() == Path.home() / "elsewhere"

    @pytest.mark.parametrize("value", ["", "   "])
    def test_an_empty_override_is_not_a_choice(self, value, monkeypatch):
        monkeypatch.setenv(home.HOME_VARIABLE, value)
        assert home.directory() == Path.home() / home.DIRECTORY_NAME


class TestCreatingIt:
    def test_it_is_created_when_missing(self, tmp_path, monkeypatch):
        target = tmp_path / "nested" / ".cumo_db_schema_comparer"
        monkeypatch.setenv(home.HOME_VARIABLE, str(target))
        assert home.ensure() == target
        assert target.is_dir()

    def test_creating_it_twice_is_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv(home.HOME_VARIABLE, str(tmp_path / "home"))
        assert home.ensure() is not None
        assert home.ensure() is not None

    def test_it_belongs_to_one_person(self, tmp_path, monkeypatch):
        """0o700: the files are not secret, but nothing in here is anyone else's."""
        target = tmp_path / "home"
        monkeypatch.setenv(home.HOME_VARIABLE, str(target))
        home.ensure()
        assert oct(target.stat().st_mode & 0o777) == "0o700"

    def test_a_folder_that_cannot_be_made_does_not_stop_the_application(
        self, tmp_path, monkeypatch
    ):
        """A read-only home must not be fatal: every path here can be typed instead."""
        blocked = tmp_path / "a-file"
        blocked.write_text("not a directory")
        monkeypatch.setenv(home.HOME_VARIABLE, str(blocked / "home"))
        assert home.ensure() is None

    def test_an_existing_configuration_is_left_alone(self, tmp_path, monkeypatch):
        monkeypatch.setenv(home.HOME_VARIABLE, str(tmp_path))
        kept = tmp_path / "invoicing.yaml"
        kept.write_text("version: 1")
        home.ensure()
        assert kept.read_text() == "version: 1"


class TestWhereANewConfigurationGoes:
    """One file. The window is not asked for a name, and it cannot open what it wrote, so there is
    nothing for a second filename to distinguish."""

    def test_it_is_one_known_file_in_the_folder(self, tmp_path, monkeypatch):
        monkeypatch.setenv(home.HOME_VARIABLE, str(tmp_path))
        assert home.default_path() == tmp_path / "config.yaml"

    def test_saving_again_writes_the_same_file(self, tmp_path, monkeypatch):
        """Which means a second save replaces the first, and nothing in the window reopens it."""
        monkeypatch.setenv(home.HOME_VARIABLE, str(tmp_path))
        assert home.default_path() == home.default_path()
