"""The release pipeline: what it promises to build, and the files it builds it from.

Nothing here runs a build. A release is made once in a while, from a tag, and a mistake in it costs
a failed run after a long compile -- or worse, an artifact that is not what its name says -- so the
parts that can be checked without compiling are checked here.
"""

from __future__ import annotations

import re
import struct
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
NFPM = yaml.safe_load((ROOT / "packaging/linux/nfpm.yaml").read_text(encoding="utf-8"))
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def matrix(job: str) -> list[dict[str, str]]:
    return WORKFLOW["jobs"][job]["strategy"]["matrix"]["include"]


class TestPlatforms:
    def test_linux_is_built_natively_for_both_architectures(self):
        # Nuitka does not cross-compile, so an arm64 package needs an arm64 runner.
        arches = {row["arch"]: row["runner"] for row in matrix("linux")}
        assert set(arches) == {"amd64", "arm64"}
        assert arches["arm64"].endswith("-arm")

    def test_windows_is_built_for_x64_only_until_its_dependencies_have_arm64_wheels(self):
        """psycopg-binary and slint publish no win_arm64 wheel, so an arm64 job cannot install.

        The x64 build runs on Windows on Arm under emulation. When both wheels exist, add the
        arm64 runner back and replace this with the two-architecture check.
        """
        assert [row["arch"] for row in matrix("windows")] == ["x64"]

    def test_macos_is_built_on_both_architectures_and_then_joined(self):
        assert {row["arch"] for row in matrix("macos-build")} == {"x64", "arm64"}
        assert set(WORKFLOW["jobs"]["macos-universal"]["needs"]) >= {"macos-build"}

    def test_the_release_waits_for_every_platform(self):
        needs = set(WORKFLOW["jobs"]["release"]["needs"])
        assert needs >= {"linux", "windows", "macos-universal"}

    def test_a_release_is_published_only_from_a_version_tag(self):
        assert "refs/tags/v" in WORKFLOW["jobs"]["release"]["if"]
        assert WORKFLOW[True]["push"]["tags"] == ["v*"]  # `on` parses as the boolean True

    def test_only_the_release_job_may_write(self):
        assert WORKFLOW["permissions"] == {"contents": "read"}
        assert WORKFLOW["jobs"]["release"]["permissions"] == {"contents": "write"}
        writers = [n for n, j in WORKFLOW["jobs"].items() if "permissions" in j]
        assert writers == ["release"]

    def test_every_action_is_pinned_to_a_version(self):
        text = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        for used in re.findall(r"uses: (\S+)", text):
            assert re.search(r"@v\d", used), f"{used} is not pinned"


class TestVersioning:
    def test_the_package_version_is_stated_in_one_value_per_place_and_they_agree(self):
        init = (ROOT / "src/db_schema_diff/__init__.py").read_text(encoding="utf-8")
        found = re.search(r'__version__ = "([^"]+)"', init)
        assert found is not None
        assert found.group(1) == PYPROJECT["project"]["version"]

    def test_the_workflow_checks_the_tag_against_the_package(self):
        text = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        assert "does not match the package version" in text


class TestLinuxPackage:
    def destinations(self) -> dict[str, str]:
        return {item["dst"]: item["src"] for item in NFPM["contents"]}

    def test_both_programs_are_installed_on_the_path(self):
        installed = self.destinations()
        assert installed["/usr/bin/db-schema-diff-gui"] == "build/db-schema-diff-gui"
        assert installed["/usr/bin/db-schema-diff"] == "build/db-schema-diff"

    def test_the_binaries_are_named_as_the_build_names_them(self):
        scripts = PYPROJECT["project"]["scripts"]
        assert {"db-schema-diff", "db-schema-diff-gui"} <= set(scripts)
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        assert "--output-filename=db-schema-diff-gui" in makefile
        assert "CLI_NAME := db-schema-diff\n" in makefile

    def test_every_file_that_is_not_a_build_output_exists(self):
        for item in NFPM["contents"]:
            if not item["src"].startswith("build/"):
                assert (ROOT / item["src"]).is_file(), item["src"]

    def test_the_desktop_entry_launches_the_installed_gui_with_its_installed_icon(self):
        entry = (ROOT / "packaging/linux/db-schema-diff.desktop").read_text(encoding="utf-8")
        assert "Exec=db-schema-diff-gui" in entry
        assert "Icon=db-schema-diff" in entry
        assert any(dst.endswith("/apps/db-schema-diff.png") for dst in self.destinations())

    def test_each_format_declares_the_libraries_the_binary_needs(self):
        for packager in ("deb", "rpm"):
            depends = NFPM["overrides"][packager]["depends"]
            assert any("xkbcommon" in d for d in depends), packager
            assert any("inpu" in d.lower() for d in depends), packager

    def test_the_architecture_and_version_come_from_the_environment(self):
        assert NFPM["arch"] == "${ARCH}"
        assert NFPM["version"] == "${VERSION}"


class TestWindowsIcon:
    def entries(self) -> list[tuple[int, int, int, int]]:
        data = (ROOT / "packaging/db-schema-diff-gui.ico").read_bytes()
        reserved, kind, count = struct.unpack("<HHH", data[:6])
        assert (reserved, kind) == (0, 1)
        found = []
        for index in range(count):
            width, height, _, _, _, _, size, offset = struct.unpack(
                "<BBBBHHII", data[6 + 16 * index : 22 + 16 * index]
            )
            assert data[offset : offset + 8] == b"\x89PNG\r\n\x1a\n", "entry is not a PNG"
            found.append((width, height, size, offset))
        assert found[-1][3] + found[-1][2] == len(data), "the directory does not cover the file"
        return found

    def test_the_icon_is_a_valid_multi_resolution_ico(self):
        widths = [width or 256 for width, *_ in self.entries()]
        assert 256 in widths and 16 in widths

    def test_the_build_hands_it_to_nuitka_on_windows(self):
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        assert "--windows-icon-from-ico=packaging/db-schema-diff-gui.ico" in makefile
        assert "--windows-console-mode=disable" in makefile


@pytest.mark.parametrize("job", ["linux", "windows", "macos-build"])
def test_each_build_job_builds_through_make(job):
    """One definition of the Nuitka flags: the Makefile's. A second copy here would drift."""
    steps = " ".join(step.get("run", "") for step in WORKFLOW["jobs"][job]["steps"])
    assert "make " in steps and "exe" in steps
    assert "nuitka" not in steps
