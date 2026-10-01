# Development entry points. Mirrors cumo-local-qa-env/RMV's Makefile conventions.
PY := .venv/bin/python

.PHONY: help venv venv-gui test test-integration test-all test-gui lint fmt typecheck build \
	gui clean

# The GUI needs 3.12+ (the Slint binding's floor); the CLI still supports 3.11, so the two
# environments are separate and only this one has the gui extra.
PY_GUI := .venv-gui/bin/python

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

venv: ## Create .venv and install the project with dev extras
	python3.11 -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e '.[dev]'

venv-gui: ## Create .venv-gui for the desktop application. --pre: every slint release is one.
	python3.12 -m venv .venv-gui
	$(PY_GUI) -m pip install --upgrade pip
	$(PY_GUI) -m pip install --pre -e '.[dev,gui]'

test: ## Unit tests only. No Docker required.
	$(PY) -m pytest

test-integration: ## Integration tests. Requires Docker or CUMO_SCHEMA_DIFF_TEST_DSN.
	$(PY) -m pytest -m integration

test-all: ## Every test.
	$(PY) -m pytest -m ''

test-gui: ## GUI tests. Needs the gui extra on Python 3.12+.
	$(PY_GUI) -m pytest tests/unit/gui -v

gui: ## Run the desktop application from the current checkout.
	$(PY_GUI) -m cumo_schema_comparer.gui

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

clean:
	rm -rf build dist .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
