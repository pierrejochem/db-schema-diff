"""Reading and validating config files from disk.

Pydantic produces the validation messages; this module's job is to turn them into something
that names the offending file and key path, and to expand a directory argument.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ..errors import ConfigError
from .model import ComparerConfig, IgnoreConfig

#: Extensions treated as config files when a directory is given.
YAML_SUFFIXES = (".yaml", ".yml")


def load_config_files(paths: Iterable[Path]) -> list[tuple[Path, ComparerConfig]]:
    """Load every config named by ``paths``, paired with the file it came from.

    The path is kept because ``ignores_file`` resolves relative to the config that named it, so a
    config directory can be checked out anywhere without rewriting paths inside it.

    A path may be a file or a directory; a directory contributes each ``*.yaml`` inside it, in
    sorted order so runs are reproducible.
    """
    return [(path, _load_one(path)) for path in _expand(list(paths))]


def load_config(paths: Iterable[Path]) -> list[ComparerConfig]:
    """Load every config named by ``paths``, without their source paths."""
    return [config for _, config in load_config_files(paths)]


def _expand(paths: Sequence[Path]) -> list[Path]:
    if not paths:
        raise ConfigError("No configuration given. Pass --config PATH (file or directory).")
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            found = sorted(p for p in path.iterdir() if p.suffix in YAML_SUFFIXES and p.is_file())
            if not found:
                raise ConfigError(f"{path}: no config files found (looked for *.yaml, *.yml)")
            files.extend(found)
        elif path.exists():
            files.append(path)
        else:
            raise ConfigError(f"{path}: no such file or directory")
    return files


def load_ignores(path: Path) -> IgnoreConfig:
    """Load an ignore ruleset from its own file."""
    raw = _read_yaml(path)
    try:
        return IgnoreConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(path, exc)) from exc


def resolve_ignores(config: ComparerConfig, config_path: Path | None = None) -> IgnoreConfig | None:
    """The project's own ruleset, from ``ignores_file`` or the inline ``ignores`` block.

    ``ignores_file`` resolves relative to the config that named it, so a config directory can be
    moved or checked out anywhere without rewriting paths.
    """
    if config.ignores is not None and config.ignores_file is not None:
        raise ConfigError(
            "set either ignores_file or an inline ignores block, not both; "
            "having two sources for the same rules makes it unclear which one is in force"
        )
    if config.ignores is not None:
        return config.ignores
    if config.ignores_file is None:
        return None
    base = config_path.parent if config_path is not None else Path()
    return load_ignores(base / config.ignores_file)


def resolve_output_dir(config: ComparerConfig, config_path: Path | None = None) -> Path | None:
    """Where this config says reports go, as a path. ``None`` when it does not say.

    Resolves relative to the config that named it, exactly as ``ignores_file`` does, so a config
    directory can be checked out anywhere without rewriting paths. An absolute path is left where
    it points, and ``~`` is expanded — somebody will type one, and a directory literally named
    ``~`` is not what they meant.

    The one place this decision is made. Both the command line and the desktop application call
    it, so neither can disagree with the other about where a config's reports belong.
    """
    if config.output_dir is None:
        return None
    named = Path(config.output_dir).expanduser()
    if named.is_absolute():
        return named
    base = config_path.parent if config_path is not None else Path()
    return base / named


def _load_one(path: Path) -> ComparerConfig:
    raw = _read_yaml(path)
    try:
        return ComparerConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(path, exc)) from exc


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read ({exc.strerror})") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML\n{_indent(str(exc))}") from exc
    if data is None:
        raise ConfigError(f"{path}: file is empty")
    if not isinstance(data, dict):
        raise ConfigError(
            f"{path}: expected a mapping at the top level, found {type(data).__name__}"
        )
    return data


def _format_validation_error(path: Path, exc: ValidationError) -> str:
    lines = [f"{path}: invalid configuration"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(root)"
        lines.append(f"  {location}: {error['msg']}")
    return "\n".join(lines)


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())
