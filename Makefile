# Development entry points. Mirrors local-qa-env/RMV's Makefile conventions.
PY := .venv/bin/python

.PHONY: help venv venv-gui test test-integration test-all test-gui test-gui-cov lint fmt typecheck build \
	gui exe exe-cli clean

# The GUI needs 3.12+ (the Slint binding's floor); the CLI still supports 3.11, so the two
# environments are separate and only this one has the gui extra.
PY_GUI := .venv-gui/bin/python

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# `uv` when present, because it fetches the exact interpreter on any OS -- a Linux box rarely has
# python3.11 or python3.12 on PATH. Otherwise the versioned interpreter must already be installed.
HAVE_UV := $(shell command -v uv 2>/dev/null)

# $(call mkvenv,<python version>,<dir>,<pip flags>,<extras>). The extras go in through variables:
# a literal comma inside a make argument would split it.
COMMA := ,
EXTRAS_DEV := .[dev]
EXTRAS_GUI := .[dev,gui]

ifdef HAVE_UV
mkvenv = uv venv --clear --python $(1) $(2) && uv pip install --python $(2)/bin/python $(3) -e '$(4)'
else
mkvenv = python$(1) -m venv --clear $(2) && $(2)/bin/python -m pip install --upgrade pip && \
	$(2)/bin/python -m pip install $(3) -e '$(4)'
endif

# Install into an existing venv. A venv made by uv has no pip, so `python -m pip` is not an option.
# $(call pipinstall,<python>,<pip flags>,<requirement>)
ifdef HAVE_UV
pipinstall = uv pip install -q --python $(1) $(2) -e '$(3)'
else
pipinstall = $(1) -m pip install -q $(2) -e '$(3)'
endif

venv: ## Create .venv and install the project with dev extras
	$(call mkvenv,3.11,.venv,,$(EXTRAS_DEV))

venv-gui: ## Create .venv-gui for the desktop application. --pre: every slint release is one.
	$(call mkvenv,3.12,.venv-gui,--pre,$(EXTRAS_GUI))

test: ## Unit tests only. No Docker required.
	$(PY) -m pytest

test-integration: ## Integration tests. Requires Docker or DB_SCHEMA_DIFF_TEST_DSN.
	$(PY) -m pytest -m integration

test-all: ## Every test.
	$(PY) -m pytest -m ''

test-gui: ## GUI tests. Needs the gui extra on Python 3.12+.
	$(PY_GUI) -m pytest tests/unit/gui -v

test-gui-cov: ## GUI tests with the floor the `gui` CI job applies.
	$(PY_GUI) -m pytest tests/unit/gui --cov --cov-config=.coveragerc-gui \
		--cov-report=term-missing --cov-fail-under=90

gui: ## Run the desktop application from the current checkout.
	$(PY_GUI) -m db_schema_diff.gui

lint: ## Format check, lint and type check.
	$(PY) -m ruff format --check .
	$(PY) -m ruff check .
	$(PY) -m mypy

fmt: ## Apply formatting and autofixable lint rules.
	$(PY) -m ruff format .
	$(PY) -m ruff check --fix .

typecheck: ## Type check only.
	$(PY) -m mypy

build: ## Build the wheel and sdist.
	$(PY) -m build

# --include-package-data is not optional here, whatever the Nuitka tutorials show. Everything this
# program reads at run time is package data loaded through importlib.resources -- the 14 catalog
# queries, the report templates, the default ignore ruleset, the .slint markup and the bundled
# typefaces -- and Nuitka ships none of it by default. Without the flag the build succeeds and the
# binary dies on the first query it tries to load.
#
# mypy is excluded because pydantic ships a mypy plugin, so following imports reaches the whole of
# mypy -- forty-odd modules of a dev dependency, in a shipped binary, and it fails to compile.
#
# --no-deployment-flag=self-execution because a compiled binary inherits a guard that treats `-c`
# as an attempt to re-execute itself the way `python -c` would. `-c` is this CLI's short form of
# --config, so without this the binary refuses its own primary invocation:
#     Error, the program tried to call itself with '-c' argument: 'config.example.yaml'.
NUITKA_FLAGS := --output-dir=build --assume-yes-for-downloads \
	--include-package-data=db_schema_diff --nofollow-import-to=mypy \
	--no-deployment-flag=self-execution

UNAME_S := $(shell uname -s)

# How each program is packaged, which is not the same question for the two of them.
#
# On macOS the GUI becomes a real .app: that is the only way to get NSHighResolutionCapable, which
# a Slint window visibly wants, and it is what you drag to /Applications. It has to be --standalone
# and not --onefile -- with --onefile, Nuitka 4.2.2 writes Info.plist *beside* the bundle instead
# of inside Contents/ and names a CFBundleExecutable that is not in there, so the bundle does not
# launch. Nuitka also names the bundle after the compiled script, so `main.app` gets renamed below;
# --macos-app-name only sets the display name inside the plist.
#
# The icon is generated from the design tokens by packaging/make_icon.py; without one Nuitka
# warns and the dock shows a generic placeholder.
#
# Everywhere else, and for the command-line tool on every platform, --onefile is right: one file to
# copy onto a machine that has no Python.
ifeq ($(UNAME_S),Darwin)
GUI_PACKAGING := --standalone --macos-create-app-bundle --macos-app-name="DB Schema Diff" \
	--macos-app-icon=packaging/db-schema-diff-gui.icns
GUI_ARTIFACT := build/db-schema-diff-gui.app
else
GUI_PACKAGING := --onefile --output-filename=db-schema-diff-gui
GUI_ARTIFACT := build/db-schema-diff-gui
endif

# -e is not a detail. Installing this project non-editably into a development environment puts a
# *copy* of the package in site-packages, which then shadows src/ -- Nuitka compiles the copy and
# the tests exercise the copy, both silently stale. That is exactly how the first working build
# came out missing a module that had been added minutes earlier.
exe: ## Build the GUI: a .app bundle on macOS, one file elsewhere. Needs a C toolchain; minutes.
	$(call pipinstall,$(PY_GUI),--pre,.[gui$(COMMA)exe])
	PATH="$(CURDIR)/.venv-gui/bin:$$PATH" $(PY_GUI) -m nuitka $(NUITKA_FLAGS) $(GUI_PACKAGING) main.py
ifeq ($(UNAME_S),Darwin)
	rm -rf '$(GUI_ARTIFACT)'
	mv build/main.app '$(GUI_ARTIFACT)'
endif
	@echo "built $(GUI_ARTIFACT)"

exe-cli: ## Build a single-file CLI executable. Runs on the 3.11 environment, like the CLI itself.
	$(call pipinstall,$(PY),,.[exe])
	PATH="$(CURDIR)/.venv/bin:$$PATH" $(PY) -m nuitka $(NUITKA_FLAGS) --onefile --output-filename=db-schema-diff main_cli.py

clean:
	rm -rf build dist .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
