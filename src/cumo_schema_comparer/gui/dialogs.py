"""Native file and directory pickers for the desktop application.

Slint's Python binding has no file dialog, and starting a second GUI toolkit inside its event loop
to borrow one risks taking the window down with it. Every picker here therefore runs in a
**subprocess**: the dialog belongs to another process with its own event loop, so the worst a
failure can do is return no path.

One backend per platform, each already present on a machine that can run this GUI — ``osascript``
on macOS, ``zenity`` or ``kdialog`` on a Linux desktop. Where none is available :func:`available`
says so and every field still accepts a typed path, which is how this worked before.

Windows has no backend here deliberately. The pattern would be the same through PowerShell, but it
cannot be exercised from this project's machines or its CI, and a picker that returns the wrong
thing is worse than one that is honestly absent.

The call blocks while the dialog is open, as a modal dialog does. The window does not repaint
during that time; it is the one cost of keeping the dialog out of this process.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

#: Long enough that a person browsing their filesystem is never cut off, short enough that a
#: backend which somehow never exits cannot hang the application for good.
TIMEOUT_SECONDS = 600

#: One platform's picker: a request and a starting directory in, a path or nothing out.
Backend = Callable[["Request", "Path | None"], "str | None"]

#: Read through a name rather than `sys.platform` directly, for two reasons: a type checker
#: narrows that literal to whichever platform it runs on and calls every other branch dead code,
#: and a test needs to exercise all three backends from one machine.
PLATFORM = sys.platform


@dataclass(frozen=True)
class Request:
    """What one picker asks for."""

    prompt: str
    directory: bool


#: The purposes the application asks for, by the names it uses. An unknown purpose is never
#: guessed at: a picker that opens the wrong kind of dialog is a worse answer than none.
REQUESTS: dict[str, Request] = {
    "config": Request("Open a configuration file", directory=False),
    "baseline": Request("Choose a baseline report", directory=False),
    "output-directory": Request("Choose where to write the reports", directory=True),
}


def _run(argv: list[str]) -> str | None:
    """One line of output from a picker, or ``None`` for cancel, failure or absence.

    Never a shell: the argument vector carries the paths, so nothing a filename contains can be
    read as a command. A cancelled dialog exits non-zero on every backend here, which is why
    cancel and failure look the same and the caller distinguishes them with :func:`available`.
    """
    try:
        finished = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if finished.returncode != 0:
        return None
    chosen = finished.stdout.strip()
    return chosen or None


def _applescript_string(value: str) -> str:
    """``value`` as an AppleScript string literal.

    The prompt and the starting directory are interpolated into a script rather than passed as
    arguments, so a path holding a quote or a backslash would otherwise end the literal and have
    the rest of the path evaluated as AppleScript.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _start_directory(current: str) -> Path | None:
    """Where the dialog should open, taken from whatever the field already holds.

    A directory opens there, a file opens beside itself, and anything else — empty, typed but not
    yet created, deleted since — opens wherever the backend defaults to rather than failing.
    """
    text = current.strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if path.is_dir():
        return path
    parent = path.parent
    return parent if parent.is_dir() and str(parent) != "." else None


def _macos(request: Request, start: Path | None) -> str | None:
    verb = "choose folder" if request.directory else "choose file"
    script = f"POSIX path of ({verb} with prompt {_applescript_string(request.prompt)}"
    if start is not None:
        script += f" default location (POSIX file {_applescript_string(str(start))})"
    script += ")"
    return _run(["osascript", "-e", script])


def _zenity(request: Request, start: Path | None) -> str | None:
    argv = ["zenity", "--file-selection", f"--title={request.prompt}"]
    if request.directory:
        argv.append("--directory")
    if start is not None:
        # The trailing separator is what tells zenity this is the directory to open, rather than
        # a file of that name to preselect.
        argv.append(f"--filename={start}/")
    return _run(argv)


def _kdialog(request: Request, start: Path | None) -> str | None:
    flag = "--getexistingdirectory" if request.directory else "--getopenfilename"
    return _run(["kdialog", "--title", request.prompt, flag, str(start or Path.home())])


def _backend() -> tuple[str, Backend] | None:
    """The tool to use and the function that drives it, or ``None`` on a machine with neither."""
    if PLATFORM == "darwin":
        return ("osascript", _macos) if shutil.which("osascript") else None
    if PLATFORM.startswith("win"):
        return None
    for tool, driver in (("zenity", _zenity), ("kdialog", _kdialog)):
        if shutil.which(tool):
            return tool, driver
    return None


def available() -> bool:
    """Whether this machine can show a picker at all.

    The caller needs this to tell a cancelled dialog from an absent one: both produce no path, but
    only one of them is worth a message about typing it instead.
    """
    return _backend() is not None


def choose(purpose: str, current: str) -> str | None:
    """A path for ``purpose``, starting from ``current``, or ``None``.

    ``None`` covers cancel, failure and no backend. :func:`available` separates the last of those.
    """
    request = REQUESTS.get(purpose)
    if request is None:
        return None
    backend = _backend()
    if backend is None:
        return None
    _, driver = backend
    return driver(request, _start_directory(current))
