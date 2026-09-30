"""The configuration the Config tab edits, read from and written to the CLI's own YAML.

A saved file must stay hand-editable, so only the fields the file actually set are written back.
Loading an untouched file and saving it again produces the same bytes; comments are the one thing
that cannot survive a YAML round trip, which is what :meth:`ConfigDocument.had_comments` is for.

The file is non-secret and committed to git, so a document refuses to be written when a value
looks like a pasted connection string (a URL, or ``keyword=value`` pairs) or when ``dsn_env`` is
not an environment variable name. That is a guard against one specific mistake, a DSN pasted into
the wrong box, and nothing more: it is not a promise that no secret can reach the file, since a
password typed into a free-text field is indistinguishable from any other text. Errors name the
field by position and never quote the value.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ..config.loader import load_config_files
from ..config.model import ComparerConfig, SourceRef
from ..errors import ConfigError
from .errors import GuiError
from .shape import looks_like_connection_string

#: Keys of a source, in the order a person writes them: identity, then connection, then scope.
_SOURCE_KEY_ORDER = ("label", "host", "database", "dsn_env", "schemas", "schema_map", "liquibase")
_OPTIONAL_TEXT_FIELDS = ("host", "database")
#: POSIX environment variable name. The config holds names, never DSNs, and this is what keeps a
#: pasted connection string from being written into a file that is committed to git.
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class _IndentedDumper(yaml.SafeDumper):
    """Indent block sequences under their key, so list edits produce reviewable diffs."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow, False)


@dataclass
class ConfigDocument:
    """A configuration being edited, plus whether it has unsaved changes."""

    path: Path | None
    config: ComparerConfig
    dirty: bool = False
    _had_comments: bool = field(default=False, repr=False)
    _key_order: Any = field(default=None, repr=False, compare=False)

    @classmethod
    def load(cls, path: Path) -> ConfigDocument:
        if path.is_dir():
            raise GuiError(f"{path}: a single config file is required, not a directory")
        try:
            loaded = load_config_files([path])
        except ConfigError as exc:
            raise GuiError(str(exc)) from exc
        ((_, config),) = loaded
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise GuiError(f"{path}: cannot read ({exc.strerror})") from exc
        try:
            key_order = yaml.safe_load(text)
        except yaml.YAMLError:  # pragma: no cover - the loader already parsed this text
            key_order = None
        commented = any(line.lstrip().startswith("#") for line in text.splitlines())
        return cls(
            path=path, config=config, dirty=False, _had_comments=commented, _key_order=key_order
        )

    @classmethod
    def blank(cls, name: str = "new-service") -> ConfigDocument:
        config = ComparerConfig(
            version=1,
            name=name,
            master=SourceRef(label="prod", dsn_env="PROD_DSN"),
            targets=(SourceRef(label="qa", dsn_env="QA_DSN"),),
        )
        return cls(path=None, config=config, dirty=False)

    def had_comments(self) -> bool:
        """Whether the file as loaded had comment lines that a save will drop."""
        return self._had_comments

    def to_yaml(self) -> str:
        """The YAML for this document. Refuses an invalid one rather than writing it."""
        self._require_valid(self.config)
        return self._render(self.config)

    def _require_valid(self, config: ComparerConfig) -> None:
        errors = _problems(config)
        if errors:
            summary = "; ".join(f"{e.field}: {e}" if e.field else str(e) for e in errors)
            raise GuiError(f"cannot write an invalid configuration ({summary})", errors[0].field)

    def _render(self, config: ComparerConfig) -> str:
        data = config.model_dump(mode="json", exclude_unset=True, by_alias=True)
        ordered: dict[str, Any] = {"version": data.pop("version", 1)}
        ordered.update(data)
        if "master" in ordered:
            ordered["master"] = _order_source(ordered["master"])
        if "targets" in ordered:
            ordered["targets"] = [_order_source(t) for t in ordered["targets"]]
        ordered = _follow(ordered, self._key_order)
        return yaml.dump(
            ordered,
            Dumper=_IndentedDumper,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        )

    def save(self, path: Path | None = None) -> None:
        target = path or self.path
        if target is None:
            raise GuiError("no file to save to; choose a path first")
        config = self._for_location(target)
        self._require_valid(config)
        text = self._render(config)
        tmp_name: str | None = None
        try:
            fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            if target.exists():
                os.chmod(tmp_name, target.stat().st_mode & 0o777)
            os.replace(tmp_name, target)
            tmp_name = None
        except OSError as exc:
            raise GuiError(f"{target}: cannot save ({exc.strerror or exc})") from exc
        finally:
            if tmp_name is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
        self.config = config
        self.path = target
        self.dirty = False

    def _for_location(self, target: Path) -> ComparerConfig:
        """The config as it must read when stored at ``target``.

        ``ignores_file`` is relative to the config file, so it is rewritten when the directory
        changes; absolute if no relative spelling exists.
        """
        ref = self.config.ignores_file
        if ref is None or self.path is None or os.path.isabs(ref):
            return self.config
        old_dir = self.path.parent.resolve()
        new_dir = target.parent.resolve()
        if old_dir == new_dir:
            return self.config
        resolved = old_dir / ref
        try:
            moved = os.path.relpath(resolved, new_dir)
        except ValueError:
            moved = str(resolved)
        return self.config.model_copy(update={"ignores_file": moved})

    def validate(self) -> list[GuiError]:
        return _problems(self.config)

    def source_rows(self) -> list[dict[str, Any]]:
        return [
            {
                "label": source.label,
                "is_master": source is self.config.master,
                "dsn_env": source.dsn_env,
                "host": source.host or "",
                "database": source.database or "",
                "schemas": list(source.schemas) if source.schemas is not None else [],
            }
            for source in self.config.sources
        ]

    def update_source(self, label: str, field: str, value: Any) -> None:
        """Change one field of the source named ``label``.

        The result is not validated here: a form passes through half-typed states, and
        :meth:`validate` reports them against the field.
        """
        if field not in SourceRef.model_fields:
            raise GuiError(f"unknown source field {field!r}", field=field)
        if field in _OPTIONAL_TEXT_FIELDS and value == "":
            value = None
        if field == "dsn_env" and isinstance(value, str):
            value = value.strip()
        if field == "schemas":
            items = value.split(",") if isinstance(value, str) else (value or ())
            names = tuple(n.strip() for n in items if n.strip())
            value = names or None
        config = self.config
        if config.master.label == label:
            master = config.master.model_copy(update={field: value})
            self.config = config.model_copy(update={"master": master})
        else:
            targets = tuple(
                t.model_copy(update={field: value}) if t.label == label else t
                for t in config.targets
            )
            if targets == config.targets and label not in {t.label for t in config.targets}:
                raise GuiError(f"no source labelled {label!r}")
            self.config = config.model_copy(update={"targets": targets})
        self.dirty = True

    def add_target(self, label: str) -> None:
        if not label or any(c.isspace() for c in label):
            raise GuiError("a label must be non-empty and contain no whitespace", field="label")
        if label in {s.label for s in self.config.sources}:
            raise GuiError(f"a source labelled {label!r} already exists", field="label")
        env = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_").upper() or "TARGET"
        target = SourceRef(label=label, dsn_env=f"{env}_DSN")
        self.config = self.config.model_copy(update={"targets": (*self.config.targets, target)})
        self.dirty = True

    def remove_target(self, label: str) -> None:
        if label == self.config.master.label:
            raise GuiError(f"{label!r} is the master and cannot be removed")
        remaining = tuple(t for t in self.config.targets if t.label != label)
        if len(remaining) == len(self.config.targets):
            raise GuiError(f"no target labelled {label!r}")
        self.config = self.config.model_copy(update={"targets": remaining})
        self.dirty = True


def _order_source(data: dict[str, Any]) -> dict[str, Any]:
    known = {k: data[k] for k in _SOURCE_KEY_ORDER if k in data}
    known.update({k: v for k, v in data.items() if k not in known})
    return known


def _follow(data: Any, reference: Any) -> Any:
    """``data`` with mapping keys in the order the loaded file used, so a re-save is a no-op.

    Keys the file did not have keep their canonical position after those it did. List items are
    matched to the reference by ``label`` where they have one, otherwise by position.
    """
    if isinstance(data, dict) and isinstance(reference, dict):
        seen = [k for k in reference if k in data]
        rest = [k for k in data if k not in reference]
        return {k: _follow(data[k], reference.get(k)) for k in (*seen, *rest)}
    if isinstance(data, list) and isinstance(reference, list):
        by_label = {r["label"]: r for r in reference if isinstance(r, dict) and "label" in r}
        out = []
        for index, item in enumerate(data):
            if isinstance(item, dict) and "label" in item:
                ref = by_label.get(item["label"])
            else:
                ref = reference[index] if index < len(reference) else None
            out.append(_follow(item, ref))
        return out
    return data


def _string_fields(node: Any, path: str = "") -> list[tuple[str, str]]:
    """Every string that would be written, with the path of the field that holds it.

    Mapping keys of user data are strings too; they are reported against their mapping, never by
    their own text, because the text is what must not be quoted.
    """
    found: list[tuple[str, str]] = []
    if isinstance(node, str):
        found.append((path, node))
    elif isinstance(node, dict):
        for key, child in node.items():
            in_map = path.endswith("schema_map")
            if in_map:
                found.append((path, key))
            child_path = path if in_map else f"{path}.{key}".lstrip(".")
            found.extend(_string_fields(child, child_path))
    elif isinstance(node, list | tuple):
        for index, child in enumerate(node):
            found.extend(_string_fields(child, f"{path}.{index}"))
    return found


def _problems(config: ComparerConfig) -> list[GuiError]:
    """Everything wrong with ``config``, never quoting a value: it may be a credential."""
    errors: list[GuiError] = []
    try:
        ComparerConfig.model_validate(config.model_dump(by_alias=True))
    except ValidationError as exc:
        errors = [
            GuiError(str(e["msg"]), field=".".join(str(p) for p in e["loc"]) or None)
            for e in exc.errors()
        ]
    reported = {e.field for e in errors}
    for location, text in _string_fields(config.model_dump(mode="json", by_alias=True)):
        if location not in reported and looks_like_connection_string(text):
            reported.add(location)
            errors.append(
                GuiError(
                    f"{_describe(location)}: a connection string does not belong here; "
                    "the configuration holds names only",
                    field=location,
                )
            )
    named = [
        ("master", config.master),
        *((f"targets.{i}", t) for i, t in enumerate(config.targets)),
    ]
    for prefix, source in named:
        location = f"{prefix}.dsn_env"
        if location not in reported and not _ENV_NAME.fullmatch(source.dsn_env):
            errors.append(
                GuiError(
                    f"{_describe(location)}: must be an environment variable name "
                    "(letters, digits and underscores), not a connection string",
                    field=location,
                )
            )
    return errors


def _describe(location: str) -> str:
    """``targets.2.host`` as ``target 3, host``: positions, because labels may hold the secret."""
    parts = location.split(".")
    if parts[0] == "targets" and len(parts) > 1 and parts[1].isdigit():
        return f"target {int(parts[1]) + 1}, {'.'.join(parts[2:]) or 'entry'}"
    if parts[0] == "master":
        return f"master, {'.'.join(parts[1:]) or 'entry'}"
    return location
