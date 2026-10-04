---
title: "Install DB Schema Diff on Linux, macOS and Windows"
description: "Install the DB Schema Diff desktop app and command-line tool: .deb and .rpm packages, a macOS universal disk image, a Windows build, or pip."
---

# Install

DB Schema Diff ships as a desktop application and a command-line tool, both inside every release.
Pick the artifact for your system from the
[latest release](https://github.com/pierrejochem/db-schema-diff/releases/latest).

## Linux

```sh
# Debian, Ubuntu
sudo apt install ./db-schema-diff_<version>_amd64.deb

# Fedora, RHEL, openSUSE
sudo dnf install ./db-schema-diff-<version>-1.x86_64.rpm
```

Use the `arm64` and `aarch64` files on an ARM machine. The package installs `db-schema-diff` and
`db-schema-diff-gui` on the `PATH`, a menu entry and an icon, and pulls in the windowing libraries
the application needs.

## macOS

Open `db-schema-diff-<version>-macos-universal.dmg` and drag the app to `Applications`. One build
runs on Intel and on Apple silicon. It is not notarized, so the first launch needs a right-click and
**Open**.

## Windows

Unpack `db-schema-diff-<version>-windows-x64.zip`. It holds `db-schema-diff-gui.exe` and
`db-schema-diff.exe`. Windows SmartScreen warns on the first launch because the build is not
signed. On Windows on Arm the x64 build runs under emulation.

## With pip

Requires Python 3.11 or newer. The interpreter that ships with macOS (3.9) will not work.

On the server side it inspects **PostgreSQL 11 or newer**. An older server is refused by
name with exit 2 rather than failing on a catalog column it does not have. An 11 cannot say
whether a column is generated — `pg_attribute.attgenerated` arrived in 12 — so comparing an
11 against a newer major stops comparing that one attribute and says so in a note, instead
of reporting every generated column as drift.

```sh
make venv       # .venv, Python 3.11 — the CLI and its tests
make venv-gui   # .venv-gui, Python 3.12+ — adds the desktop application
```

Both targets use [`uv`](https://docs.astral.sh/uv/) when it is installed, which downloads the exact
interpreter, so they work the same on macOS and Linux. Without `uv` they need `python3.11` and
`python3.12` on `PATH`. The desktop application runs on Linux (X11 or Wayland) as well as macOS; the
**Choose…** pickers need `zenity` or `kdialog`, and storing credentials needs a Secret Service
keyring (GNOME Keyring, KWallet) — without one the window says so and the environment is used.


## How releases are built

Pushing a tag `v<version>` runs `.github/workflows/release.yml`, which builds every artifact on a
runner of its own architecture and publishes a GitHub release with checksums. The tag must equal the
version in `pyproject.toml` and `src/db_schema_diff/__init__.py`, or the run stops before building.
Running the workflow by hand builds the same artifacts without publishing.

| Platform | Artifact |
|----------|----------|
| Linux amd64, arm64 | `db-schema-diff_<version>_<arch>.deb` and `db-schema-diff-<version>-1.<arch>.rpm`, installing both programs, a menu entry and an icon |
| macOS universal | `db-schema-diff-<version>-macos-universal.dmg` — one app for Intel and Apple silicon, built as two native builds joined by `packaging/macos_universal.py` |
| Windows x64 | `db-schema-diff-<version>-windows-x64.zip` with both executables |

There is no Windows arm64 build: `psycopg-binary` and `slint` publish no wheel for it, so its
dependencies cannot be installed. Windows on Arm runs the x64 build under emulation.

Nothing is signed or notarized, so Windows SmartScreen and macOS Gatekeeper warn on first launch.


## Standalone executables

For a machine with no Python at all, Nuitka compiles either entry point into a single file:

```sh
make exe        # the desktop application
make exe-cli    # build/db-schema-diff — the command-line tool, one file
```

On macOS `make exe` produces `build/db-schema-diff-gui.app`, a real bundle: that is the only way
to get `NSHighResolutionCapable`, without which a Slint window renders non-retina, and it is what
you drag to `/Applications`. The bundle is built `--standalone` rather than `--onefile` because with
`--onefile` Nuitka 4.2 writes `Info.plist` beside the bundle instead of inside `Contents/` and names
a `CFBundleExecutable` that is not there, so the result does not launch. It is **not** code signed
or notarized, so Gatekeeper will warn anyone who did not build it themselves. On other platforms,
and for the command-line tool everywhere, the output is a single file.

Each target installs Nuitka on demand (the `exe` extra, deliberately out of `dev`: no test or CI
job compiles anything) and needs a C toolchain — Xcode command line tools on macOS, `gcc` on Linux (`patchelf` comes with the `exe` extra). A build takes
several minutes.

The build passes `--include-package-data=db_schema_diff`, which is not optional. Everything
this program reads at run time is package data loaded through `importlib.resources`: the catalog
queries, the report templates, the bundled ignore ruleset, the `.slint` markup and the typefaces.
Nuitka ships none of it by default, so without that flag the build succeeds and the binary fails on
the first query it tries to load. There are two entry scripts rather than one because Nuitka
compiles a script into a binary, and the two programs differ: the GUI needs Python 3.12 or newer
for Slint, while the command-line tool keeps its 3.11 floor.
