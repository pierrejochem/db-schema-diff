"""Where the desktop application keeps the configurations it is given.

A comparison config is a small non-secret YAML naming environment variables, and until now the
desktop application had nowhere to put one: it took a path on the command line and saved back to it,
so a config created in the GUI had to be given a location before it could be saved at all.

``~/.cumo_db_schema_comparer`` is that place. It is created on start-up and it is where a
configuration built in the application is saved.

There is one file, :data:`CONFIG_NAME`, and the application reads it back when it starts. It still
has no Open button and still takes no path on the command line: nothing here lists or chooses a
file, because there is nothing to choose between. This module says where that one file is; reading
it is ``app.Application._reopen``.

``CUMO_SCHEMA_DIFF_HOME`` overrides the location. Tests set it, because a test that writes into the
person running it's home directory is a test that has already failed.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

#: The directory name under the user's home.
DIRECTORY_NAME = ".cumo_db_schema_comparer"

#: Points the whole of this module somewhere else. Set by tests, and usable by anyone keeping their
#: configurations outside their home directory.
HOME_VARIABLE = "CUMO_SCHEMA_DIFF_HOME"


def directory() -> Path:
    """Where configurations live. Does not create anything."""
    override = os.environ.get(HOME_VARIABLE)
    if override and override.strip():
        return Path(override).expanduser()
    return Path.home() / DIRECTORY_NAME


def ensure() -> Path | None:
    """Create the directory if it is missing, and return it. ``None`` if it cannot be made.

    A home directory that is read-only, or a name already taken by a file, must not stop the
    application starting: everything it does here has a path the person can type instead.
    """
    target = directory()
    try:
        # 0o700 because this is one person's own working directory. The files are not secret —
        # they name environment variables, never values — but nothing here is anyone else's.
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("cannot create %s (%s)", target, exc.strerror or type(exc).__name__)
        return None
    if not target.is_dir():  # pragma: no cover - mkdir(exist_ok) raises when it is a file
        return None
    return target


#: The one file. The application builds a configuration, saves it, and reads it back next time; it
#: is not asked for a name, and it opens nothing else, so there is nothing for a second filename to
#: distinguish.
CONFIG_NAME = "config.yaml"


def default_path() -> Path:
    """Where a configuration is saved when it has no path of its own."""
    return directory() / CONFIG_NAME
