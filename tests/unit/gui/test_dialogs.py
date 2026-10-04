"""The native file and directory pickers.

Nothing here opens a dialog. Every test drives the backend selection, the argument vectors and the
no-path paths, because a picker that opens for real in a test suite blocks it until a person
dismisses it — which is how the first wiring of this was written, and how it was caught.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from db_schema_diff.gui import dialogs


@pytest.fixture
def recorded(monkeypatch):
    """Captures the argument vector a backend builds, instead of running it."""
    calls: list[list[str]] = []

    def fake(argv):
        calls.append(argv)
        return "/chosen/path"

    monkeypatch.setattr(dialogs, "_run", fake)
    return calls


def on_platform(monkeypatch, platform, *, found=()):
    monkeypatch.setattr(dialogs, "PLATFORM", platform)
    monkeypatch.setattr(dialogs.shutil, "which", lambda tool: tool if tool in found else None)


# -- the contract with the application ---------------------------------------------------------
def test_the_purposes_are_exactly_the_ones_the_application_asks_for():
    """A renamed purpose on either side would disable a picker and raise nothing.

    `choose` returns None for a purpose it does not know, which the application reads as "no
    dialog here" — so a typo would quietly restore the behaviour this module exists to replace.
    """
    import re

    source = (Path(dialogs.__file__).with_name("app.py")).read_text(encoding="utf-8")
    asked = set(re.findall(r'self\.choose_path\(\s*"([^"]+)"', source))
    assert asked == set(dialogs.REQUESTS), f"application asks for {asked}"


def test_only_the_output_directory_asks_for_a_directory():
    directories = {name for name, request in dialogs.REQUESTS.items() if request.directory}
    assert directories == {"output-directory"}


def test_an_unknown_purpose_opens_nothing(monkeypatch):
    monkeypatch.setattr(dialogs, "_run", lambda argv: pytest.fail("ran a dialog"))
    assert dialogs.choose("not-a-purpose", "/nowhere") is None


# -- where the dialog opens --------------------------------------------------------------------
class TestStartDirectory:
    def test_a_directory_opens_in_itself(self, tmp_path):
        assert dialogs._start_directory(str(tmp_path)) == tmp_path

    def test_a_file_opens_beside_itself(self, tmp_path):
        report = tmp_path / "baseline.json"
        report.write_text("{}", encoding="utf-8")
        assert dialogs._start_directory(str(report)) == tmp_path

    def test_a_path_not_yet_created_opens_in_its_parent(self, tmp_path):
        assert dialogs._start_directory(str(tmp_path / "not-written-yet.json")) == tmp_path

    @pytest.mark.parametrize("value", ["", "   "])
    def test_an_empty_field_lets_the_backend_choose(self, value):
        assert dialogs._start_directory(value) is None

    def test_a_bare_name_lets_the_backend_choose(self):
        # "." is not a useful place to open, and is what Path("x").parent gives.
        assert dialogs._start_directory("baseline.json") is None

    def test_a_tilde_is_expanded(self):
        assert dialogs._start_directory("~") == Path.home()


# -- the argument vectors ----------------------------------------------------------------------
class TestMacos:
    def test_a_file_picker_names_the_prompt_and_the_starting_directory(
        self, monkeypatch, recorded, tmp_path
    ):
        on_platform(monkeypatch, "darwin", found={"osascript"})
        assert dialogs.choose("baseline", str(tmp_path)) == "/chosen/path"
        argv = recorded[0]
        assert argv[:2] == ["osascript", "-e"]
        assert "choose file with prompt" in argv[2]
        assert f'default location (POSIX file "{tmp_path}")' in argv[2]
        assert argv[2].startswith("POSIX path of (")

    def test_a_directory_picker_chooses_a_folder(self, monkeypatch, recorded, tmp_path):
        on_platform(monkeypatch, "darwin", found={"osascript"})
        dialogs.choose("output-directory", str(tmp_path))
        assert "choose folder with prompt" in recorded[0][2]

    def test_an_empty_field_omits_the_starting_directory(self, monkeypatch, recorded):
        on_platform(monkeypatch, "darwin", found={"osascript"})
        dialogs.choose("baseline", "")
        assert "default location" not in recorded[0][2]


class TestAppleScriptQuoting:
    """The prompt and the path are interpolated into a script, so they must not be able to end it.

    AppleScript is the one backend that cannot take its arguments as a vector, which makes this the
    one place a filename could be read as code.
    """

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("plain", '"plain"'),
            ('has"quote', '"has\\"quote"'),
            ("has\\backslash", '"has\\\\backslash"'),
        ],
    )
    def test_escaping(self, value, expected):
        assert dialogs._applescript_string(value) == expected

    def test_a_path_cannot_close_the_literal_and_add_a_statement(
        self, monkeypatch, recorded, tmp_path
    ):
        # No separator in the name: this has to be one directory, not a nested path.
        hostile = tmp_path / 'x" & (do shell script "id") & "'
        hostile.mkdir()
        on_platform(monkeypatch, "darwin", found={"osascript"})
        dialogs.choose("baseline", str(hostile))
        script = recorded[0][2]
        # Every quote the path contributed is escaped, so the literal still closes where the
        # script says it does and `do shell script` stays inside it as text.
        assert 'do shell script \\"id\\"' in script
        assert script.count('"') - script.count('\\"') == 4  # prompt and location, two each


class TestLinux:
    def test_zenity_marks_a_directory_and_opens_in_place(self, monkeypatch, recorded, tmp_path):
        on_platform(monkeypatch, "linux", found={"zenity"})
        dialogs.choose("output-directory", str(tmp_path))
        argv = recorded[0]
        assert argv[0] == "zenity"
        assert "--directory" in argv
        # The trailing separator is what makes zenity treat this as the directory to open.
        assert f"--filename={tmp_path}/" in argv

    def test_zenity_does_not_mark_a_file_picker_as_a_directory(
        self, monkeypatch, recorded, tmp_path
    ):
        on_platform(monkeypatch, "linux", found={"zenity"})
        dialogs.choose("baseline", str(tmp_path))
        assert "--directory" not in recorded[0]

    def test_kdialog_is_used_when_zenity_is_absent(self, monkeypatch, recorded, tmp_path):
        on_platform(monkeypatch, "linux", found={"kdialog"})
        dialogs.choose("baseline", str(tmp_path))
        assert recorded[0][0] == "kdialog"
        assert "--getopenfilename" in recorded[0]

    def test_kdialog_asks_for_an_existing_directory(self, monkeypatch, recorded, tmp_path):
        on_platform(monkeypatch, "linux", found={"kdialog"})
        dialogs.choose("output-directory", str(tmp_path))
        assert "--getexistingdirectory" in recorded[0]

    def test_zenity_wins_when_both_are_installed(self, monkeypatch, recorded, tmp_path):
        on_platform(monkeypatch, "linux", found={"zenity", "kdialog"})
        dialogs.choose("baseline", str(tmp_path))
        assert recorded[0][0] == "zenity"


# -- which machines have a picker --------------------------------------------------------------
class TestAvailable:
    def test_macos_with_osascript(self, monkeypatch):
        on_platform(monkeypatch, "darwin", found={"osascript"})
        assert dialogs.available() is True

    def test_macos_without_osascript(self, monkeypatch):
        on_platform(monkeypatch, "darwin")
        assert dialogs.available() is False

    @pytest.mark.parametrize("tool", ["zenity", "kdialog"])
    def test_a_linux_desktop_with_either_tool(self, monkeypatch, tool):
        on_platform(monkeypatch, "linux", found={tool})
        assert dialogs.available() is True

    def test_a_headless_linux_box_has_none(self, monkeypatch):
        on_platform(monkeypatch, "linux")
        assert dialogs.available() is False
        assert dialogs.choose("baseline", "/nowhere") is None

    def test_windows_has_no_backend_and_says_so(self, monkeypatch):
        # Deliberately absent rather than guessed at: the PowerShell equivalent cannot be
        # exercised from this project's machines or its CI.
        on_platform(monkeypatch, "win32", found={"zenity", "kdialog", "osascript"})
        assert dialogs.available() is False
        assert dialogs.choose("baseline", "/nowhere") is None


# -- what the subprocess can do ----------------------------------------------------------------
class TestRunning:
    def completed(self, returncode, stdout):
        return subprocess.CompletedProcess(args=["x"], returncode=returncode, stdout=stdout)

    def test_a_chosen_path_comes_back_without_its_newline(self, monkeypatch):
        monkeypatch.setattr(
            dialogs.subprocess, "run", lambda *a, **k: self.completed(0, "/a/path\n")
        )
        assert dialogs._run(["x"]) == "/a/path"

    def test_a_cancelled_dialog_is_no_path(self, monkeypatch):
        # Every backend here exits non-zero on cancel.
        monkeypatch.setattr(dialogs.subprocess, "run", lambda *a, **k: self.completed(1, ""))
        assert dialogs._run(["x"]) is None

    def test_success_with_no_output_is_no_path(self, monkeypatch):
        monkeypatch.setattr(dialogs.subprocess, "run", lambda *a, **k: self.completed(0, "  \n"))
        assert dialogs._run(["x"]) is None

    def test_a_missing_tool_is_no_path_rather_than_a_crash(self, monkeypatch):
        def missing(*a, **k):
            raise FileNotFoundError("zenity")

        monkeypatch.setattr(dialogs.subprocess, "run", missing)
        assert dialogs._run(["x"]) is None

    def test_a_dialog_that_never_exits_is_no_path(self, monkeypatch):
        def hangs(*a, **k):
            raise subprocess.TimeoutExpired(cmd="x", timeout=dialogs.TIMEOUT_SECONDS)

        monkeypatch.setattr(dialogs.subprocess, "run", hangs)
        assert dialogs._run(["x"]) is None

    def test_the_timeout_leaves_room_for_a_person_to_browse(self):
        # A dialog is open while someone decides. Anything impatient here would cancel on them.
        assert dialogs.TIMEOUT_SECONDS >= 300

    def test_no_dialog_is_ever_run_through_a_shell(self, monkeypatch):
        """A filename is attacker-adjacent data; through a shell it would be a command."""
        seen: dict[str, object] = {}

        def record(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return self.completed(0, "/p")

        monkeypatch.setattr(dialogs.subprocess, "run", record)
        dialogs._run(["zenity", "--file-selection"])
        assert isinstance(seen["argv"], list)
        assert seen["kwargs"].get("shell", False) is False
        assert seen["kwargs"]["check"] is False
