"""The configuration the Config tab edits, read from and written to the CLI's own YAML.

A saved file must stay hand-editable, so only the fields the file actually set are written back.
Loading an untouched file and saving it again produces the same bytes; comments are the one thing
that cannot survive a YAML round trip, which is what :meth:`ConfigDocument.had_comments` is for.
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

#: Keys of a source, in the order a person writes them: identity, then connection, then scope.
_SOURCE_KEY_ORDER = ("label", "host", "database", "dsn_env", "schemas", "schema_map", "liquibase")
_OPTIONAL_TEXT_FIELDS = ("host", "database")


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
        try:
            ((_, config),) = load_config_files([path])
        except ConfigError as exc:
            raise GuiError(str(exc)) from exc
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
        data = self.config.model_dump(mode="json", exclude_unset=True, by_alias=True)
        ordered: dict[str, Any] = {"version": data.pop("version", 1)}
        ordered.update(data)
        if "master" in ordered:
            ordered["master"] = _order_source(ordered["master"])
        if "targets" in ordered:
            ordered["targets"] = [_order_source(t) for t in ordered["targets"]]
        ordered = _follow(ordered, self._key_order)
        return yaml.safe_dump(
            ordered, sort_keys=False, allow_unicode=True, default_flow_style=False
        )

    def save(self, path: Path | None = None) -> None:
        target = path or self.path
        if target is None:
            raise GuiError("no file to save to; choose a path first")
        text = self.to_yaml()
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
        self.path = target
        self.dirty = False

    def validate(self) -> list[GuiError]:
        try:
            ComparerConfig.model_validate(self.config.model_dump(by_alias=True))
        except ValidationError as exc:
            return [
                GuiError(str(e["msg"]), field=".".join(str(p) for p in e["loc"]) or None)
                for e in exc.errors()
            ]
        return []

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
        if field in _OPTIONAL_TEXT_FIELDS and value == "":
            value = None
        if field == "schemas":
            value = tuple(value) if value else None
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
