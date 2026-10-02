"""The bundled brand faces, and the ordering rule that makes them take effect.

These run on 3.11 as well as under the GUI extra: whether the files ship is a packaging question,
and a packaging mistake here passes every rendering test and shows the default face on a user's
machine.
"""

from __future__ import annotations

import os

import pytest

from cumo_schema_comparer.gui import fonts

EXPECTED_FACES = (
    "Archivo-Bold.ttf",
    "IBMPlexMono-Regular.ttf",
    "Mulish-Bold.ttf",
    "Mulish-Regular.ttf",
)


def test_the_font_directory_is_a_real_directory():
    directory = fonts.font_directory()
    assert directory is not None, "a wheel install must expose a scannable directory"
    assert directory.is_dir()


@pytest.mark.parametrize("face", EXPECTED_FACES)
def test_every_bundled_face_ships(face):
    directory = fonts.font_directory()
    assert directory is not None
    path = directory / face
    assert path.is_file(), f"{face} is missing from the package"
    # A truncated download is a plausible failure that an existence check would miss.
    assert path.stat().st_size > 20_000, f"{face} looks truncated"


def test_the_bundled_faces_are_exactly_the_expected_set():
    # Pinned so adding or dropping a weight is a deliberate edit, and so the families named in the
    # markup cannot drift from the files that back them.
    directory = fonts.font_directory()
    assert directory is not None
    assert tuple(sorted(p.name for p in directory.glob("*.ttf"))) == EXPECTED_FACES


@pytest.mark.parametrize("family", fonts.BUNDLED_FAMILIES)
def test_each_named_family_has_a_file_behind_it(family):
    directory = fonts.font_directory()
    assert directory is not None
    stem = family.replace(" ", "")
    assert any(stem.lower() in p.name.lower() for p in directory.glob("*.ttf")), (
        f"{family} is named in the design system but no bundled face provides it"
    )


def test_every_face_carries_its_licence():
    # OFL-1.1 requires the licence to travel with the font. Shipping the files without it would be
    # a licensing defect, not a tidiness one.
    directory = fonts.font_directory()
    assert directory is not None
    licences = {p.name for p in directory.glob("OFL-*.txt")}
    assert licences == {"OFL-archivo.txt", "OFL-ibmplexmono.txt", "OFL-mulish.txt"}
    for name in licences:
        assert "Copyright" in (directory / name).read_text(encoding="utf-8")


class TestInstall:
    def test_it_sets_the_variable_to_the_bundled_directory(self, monkeypatch):
        monkeypatch.delenv(fonts.FONT_PATH_VARIABLE, raising=False)
        used = fonts.install()
        assert used == fonts.font_directory()
        assert os.environ[fonts.FONT_PATH_VARIABLE] == str(used)

    def test_an_existing_value_wins(self, monkeypatch):
        # Someone pointing this at their own licensed cut of the brand faces is doing the right
        # thing; overwriting it would be wrong.
        monkeypatch.setenv(fonts.FONT_PATH_VARIABLE, "/somewhere/licensed")
        assert fonts.install() is None
        assert os.environ[fonts.FONT_PATH_VARIABLE] == "/somewhere/licensed"

    def test_an_empty_value_does_not_count_as_a_choice(self, monkeypatch):
        monkeypatch.setenv(fonts.FONT_PATH_VARIABLE, "")
        assert fonts.install() == fonts.font_directory()

    def test_no_directory_means_no_variable(self, monkeypatch):
        # A zipimported install has nothing for Slint to scan, so it must leave the variable alone
        # rather than point at a path that does not exist.
        monkeypatch.delenv(fonts.FONT_PATH_VARIABLE, raising=False)
        monkeypatch.setattr(fonts, "font_directory", lambda: None)
        assert fonts.install() is None
        assert fonts.FONT_PATH_VARIABLE not in os.environ


def test_the_entry_point_installs_fonts_before_importing_the_application():
    """Slint reads the variable when the renderer starts, so the call cannot move below the import.

    Setting it afterwards is measurably a no-op — the same string under a named family measures
    167px either way, where a loaded face gives 177px — and nothing else in the suite would notice,
    because an unresolvable family falls back silently.
    """
    from pathlib import Path

    source = Path(fonts.__file__).with_name("__main__.py").read_text(encoding="utf-8")
    install_at = source.index("fonts.install()")
    app_import_at = source.index("from .app import run")
    assert install_at < app_import_at, "fonts.install() must precede the app import"
