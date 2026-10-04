"""The standalone executables: their entry scripts and the build that produces them.

Nothing here compiles anything — a Nuitka build takes minutes and needs a C toolchain. What these
tests hold is the part that is silently wrong rather than loudly broken: a build that omits the
package data succeeds and produces a binary that dies on the first catalog query it loads, and an
entry script that imports a GUI toolkit too early produces one that renders in the wrong typeface.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

#: Entry script -> the console script it must agree with, by its pyproject name.
ENTRY_SCRIPTS = {
    "main.py": "db-schema-diff-gui",
    "main_cli.py": "db-schema-diff",
}


def imported_names(source: str) -> set[str]:
    """Every module imported at the top level of ``source`` — not inside a function."""
    names: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("script", sorted(ENTRY_SCRIPTS))
def test_each_entry_script_exists_and_runs_something(script):
    source = (ROOT / script).read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in source
    assert "raise SystemExit(main())" in source


@pytest.mark.parametrize(("script", "console_script"), sorted(ENTRY_SCRIPTS.items()))
def test_the_binaries_enter_where_the_installed_commands_enter(script, console_script):
    """A compiled binary and the pip-installed command must be the same program.

    The entry point is spelled twice — once in pyproject for the console script, once in the script
    Nuitka compiles — and nothing but this stops the two drifting into different behaviour.
    """
    target = PYPROJECT["project"]["scripts"][console_script]
    module, _, function = target.partition(":")
    source = (ROOT / script).read_text(encoding="utf-8")
    assert f"from {module} import {function}" in source, f"{script} does not enter at {target}"


def test_the_gui_entry_script_imports_no_toolkit_before_the_fonts_are_installed():
    """The ordering `gui/fonts.py` depends on, held at the one place that could break it.

    `gui/launcher.main` installs `SLINT_FONT_PATH` and only then imports the application. Slint
    reads that variable once, when the renderer starts, so an `import slint` reached while this
    module is imported makes the bundled typefaces a no-op — and nothing would look broken, it
    would just render in the system face.
    """
    names = imported_names((ROOT / "main.py").read_text(encoding="utf-8"))
    assert not any(name.split(".")[0] == "slint" for name in names)
    # And it must not reach the application module either, which imports slint itself.
    assert "db_schema_diff.gui.app" not in names


@pytest.mark.parametrize("script", sorted(ENTRY_SCRIPTS))
def test_no_entry_script_imports_a_module_named_dunder_main(script):
    """Nuitka names each module's generated C file after the module, and two cannot collide.

    A compiled program is itself `__main__`, so importing a package's `__main__` submodule makes
    Nuitka emit `module.__main__.c` twice and abort:

        AssertionError: build/main.build/module.__main__.c

    This is why the GUI's start-up lives in `gui/launcher.py` with `__main__.py` as a shim over it.
    Reverting that for tidiness would break the build and nothing else.
    """
    names = imported_names((ROOT / script).read_text(encoding="utf-8"))
    offenders = [name for name in names if name.split(".")[-1] == "__main__"]
    assert offenders == [], f"{script} imports {offenders}, which collides with its own __main__"


class TestBuildFlags:
    def flags(self) -> str:
        start = MAKEFILE.index("NUITKA_FLAGS :=")
        return MAKEFILE[start : MAKEFILE.index("\n\n", start)]

    def test_the_package_data_is_included(self):
        """The flag whose absence is invisible until the binary runs.

        Every catalog query, report template, ignore ruleset, `.slint` file and typeface is package
        data read through importlib.resources at run time. Nuitka ships none of it by default, so
        without this the build is clean and the binary dies on its first query.
        """
        assert "--include-package-data=db_schema_diff" in self.flags()

    def test_everything_is_built_into_the_build_directory(self):
        assert "--output-dir=build" in self.flags()

    def test_the_command_line_tool_is_always_one_file(self):
        # The point of it: one file to copy onto a machine with no Python.
        cli = next(line for line in MAKEFILE.splitlines() if "main_cli.py" in line)
        assert "--onefile" in cli

    def test_mypy_is_kept_out_of_the_binary(self):
        """pydantic ships a mypy plugin, so following imports reaches the whole of mypy.

        Forty-odd modules of a development dependency, compiled into a shipped executable — and it
        does not even build: the first attempt here died in the C backend partway through them.
        """
        assert "--nofollow-import-to=mypy" in self.flags()

    def test_the_binary_is_allowed_to_take_a_dash_c_option(self):
        """`-c` is this CLI's short form of --config, and a compiled binary guards against it.

        Nuitka's deployment mode treats `-c` as the program trying to re-execute itself, the way
        `python -c` would, and refuses:

            Error, the program tried to call itself with '-c' argument: 'config.example.yaml'.

        Which is to say the binary rejected its own primary invocation. Found by running it; the
        build was clean and `--version` and `--help` both worked.
        """
        assert "--no-deployment-flag=self-execution" in self.flags()

    def test_the_build_never_waits_for_an_answer(self):
        # A prompt for a toolchain download would hang a CI job or a `make` run with no tty.
        assert "--assume-yes-for-downloads" in self.flags()

    def test_both_targets_share_one_set_of_flags(self):
        # Two copies would let one binary ship the package data and the other not. Comment lines
        # are excluded: the flag is explained just above where it is set.
        live = [line for line in MAKEFILE.splitlines() if not line.lstrip().startswith("#")]
        assert sum(line.count("--include-package-data") for line in live) == 1
        assert sum(line.count("$(NUITKA_FLAGS)") for line in live) == 2

    def test_each_target_builds_on_the_interpreter_its_program_supports(self):
        """The GUI needs 3.12+ for Slint; the CLI keeps 3.11. Compiling on the wrong one either
        fails outright or produces a binary with the wrong floor baked in."""
        gui = next(
            line
            for line in MAKEFILE.splitlines()
            if "nuitka" in line and line.rstrip().endswith("main.py")
        )
        cli = next(
            line for line in MAKEFILE.splitlines() if "main_cli.py" in line and "nuitka" in line
        )
        assert "$(PY_GUI)" in gui
        assert "$(PY)" in cli and "$(PY_GUI)" not in cli

    def test_the_targets_are_declared_phony(self):
        phony = MAKEFILE[
            MAKEFILE.index(".PHONY:") : MAKEFILE.index("\n\n", MAKEFILE.index(".PHONY:"))
        ]
        assert "exe" in phony.split()
        assert "exe-cli" in phony.split()

    def test_neither_target_installs_this_project_non_editably(self):
        """A plain `pip install .` into a development environment is a trap.

        It copies the package into site-packages, where it shadows `src/` — so the next test run
        exercises the copy and the next build compiles the copy, both silently stale. The first
        build that completed here produced a binary missing a module added minutes earlier, and
        nothing about either the install or the build looked wrong.
        """
        lines = MAKEFILE.splitlines()
        # The exe targets install through the `pipinstall` macro; its definitions are where the
        # command line is spelled out, once for uv and once for pip.
        calls = [line for line in lines if "$(call pipinstall," in line and ".[" in line]
        assert calls, "the exe targets no longer install anything"
        definitions = [line for line in lines if line.startswith(("pipinstall =", "pipinstall:="))]
        assert definitions, "the install macro is gone"
        for line in definitions:
            assert " -e " in line, f"non-editable install of this project: {line.strip()}"

    def test_the_help_text_warns_what_a_build_costs(self):
        # `make help` lists these; a target that silently takes minutes and needs clang should say
        # so where someone reads it.
        target = next(line for line in MAKEFILE.splitlines() if line.startswith("exe:"))
        assert "C toolchain" in target or "minutes" in target


class TestDeclaredDependency:
    def test_nuitka_is_an_extra_rather_than_a_dev_dependency(self):
        """No test or CI job compiles anything, and the build needs a C toolchain.

        In `dev` it would be installed into every environment and every CI job for nothing.
        """
        extras = PYPROJECT["project"]["optional-dependencies"]
        assert any(spec.startswith("nuitka") for spec in extras["exe"])
        assert not any("nuitka" in spec for spec in extras["dev"])

    def test_the_pin_rules_out_releases_that_cannot_build_the_gui(self):
        # Nuitka gained Python 3.14 support in 4.2, and the GUI environment is 3.14.
        spec = next(s for s in PYPROJECT["project"]["optional-dependencies"]["exe"])
        assert ">=4.2" in spec

    def test_the_build_directory_is_ignored(self):
        # Both `python -m build` and Nuitka write here; a committed binary would be a 40MB blob.
        assert "/build/" in (ROOT / ".gitignore").read_text(encoding="utf-8")


class TestMacosAppBundle:
    """The GUI is packaged differently per platform, and the difference is not cosmetic."""

    def section(self) -> str:
        """The Darwin branch only — cut at `else`, or it swallows the other platforms' branch."""
        start = MAKEFILE.index("ifeq ($(UNAME_S),Darwin)")
        return MAKEFILE[start : MAKEFILE.index("else", start)]

    def test_macos_gets_a_real_app_bundle(self):
        """A .app is the only way to get NSHighResolutionCapable, which a Slint window wants."""
        assert "--macos-create-app-bundle" in self.section()

    def test_the_bundle_is_standalone_and_never_onefile(self):
        """With --onefile, Nuitka 4.2.2 builds a bundle that cannot launch.

        It writes Info.plist beside the bundle instead of inside Contents/, and the plist names a
        CFBundleExecutable that is not in there. Verified by building both ways: --standalone put
        the plist in Contents/ with a CFBundleExecutable that matches, --onefile left an empty
        .app and a stray plist.
        """
        darwin = self.section()
        assert "--standalone" in darwin
        assert "--onefile" not in darwin

    def test_the_bundle_is_renamed_off_the_script_name(self):
        """Nuitka names the bundle after the compiled script, which would make it `main.app`.

        --macos-app-name does not rename it; it only sets the display name inside the plist.
        """
        assert "mv build/main.app" in MAKEFILE
        assert "--macos-app-name=" in self.section()

    def test_the_bundle_carries_an_icon(self):
        """Without one Nuitka warns and the dock shows a generic placeholder.

        The icon is generated from the design tokens rather than committed as an unexplained blob,
        so it can be corrected when the brand is.
        """
        assert "--macos-app-icon=" in self.section()
        icon = ROOT / "packaging" / "db-schema-diff-gui.icns"
        assert icon.is_file(), "the icon the build points at does not exist"
        assert icon.stat().st_size > 10_000, "an icns this small is not a full icon set"
        assert (ROOT / "packaging" / "make_icon.py").is_file(), "the icon has no generator"
        assert (ROOT / "packaging" / "logo.py").is_file(), "the generator has no mark to draw"

    def test_the_icon_is_drawn_from_the_design_tokens(self):
        """One palette. An icon in its own colours would be the first thing to drift."""
        import re

        generator = (ROOT / "packaging" / "logo.py").read_text(encoding="utf-8")
        tokens = (ROOT / "src" / "db_schema_diff" / "gui" / "ui" / "tokens.slint").read_text(
            encoding="utf-8"
        )
        declared = dict(re.findall(r"out property <color> (\S+): #([0-9a-fA-F]{6});", tokens))
        for name, constant in (("heading", "NAVY"), ("accent-light", "LIME")):
            expected = declared[name].lower()
            packed = re.search(rf"^{constant} = \(([^)]+)\)", generator, re.M)
            assert packed, f"{constant} is not defined in the generator"
            channels = [int(part.strip(), 16) for part in packed.group(1).split(",")]
            assert "".join(f"{c:02x}" for c in channels) == expected, f"{constant} drifted"

    def test_other_platforms_still_get_one_file(self):
        first = MAKEFILE.index("ifeq ($(UNAME_S),Darwin)")
        start = MAKEFILE.index("else", first)
        other = MAKEFILE[start : MAKEFILE.index("endif", start)]
        assert "--onefile" in other
        assert "--macos" not in other

    def test_the_bundle_flags_never_reach_a_non_macos_build(self):
        # A --macos flag in the shared set would be passed on Linux, where it is not understood.
        assert "--macos" not in TestBuildFlags().flags()


class TestEntryScriptImports:
    def test_the_cli_entry_script_pulls_in_no_gui(self):
        """The command-line binary keeps the 3.11 floor, which the GUI dependency would break."""
        names = imported_names((ROOT / "main_cli.py").read_text(encoding="utf-8"))
        assert not any("gui" in name for name in names)
