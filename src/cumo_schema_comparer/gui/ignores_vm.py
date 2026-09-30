"""Editing an ignore ruleset.

Pick-lists are built from what the validator accepts, not from the nearest-looking enum. `statuses`
in particular comes from the three values `IgnoreRule` allows rather than from `ObjectStatus`, which
also contains `match` — a value the form must not be able to produce.

A rule that can never match is worse than no rule: it reads as coverage the team does not have.
:meth:`IgnoresDocument.validate` therefore reports every rule that could not match a finding, not
only the ones the model refuses to construct (editing bypasses the model's own validators). Errors
name a rule by position and never quote a value.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ..config.loader import resolve_ignores
from ..config.model import IgnoreConfig, IgnoreRule
from ..diff.ignores import load_default_ignores
from ..errors import ConfigError
from ..model.kinds import ObjectKind
from .config_vm import ConfigDocument, _IndentedDumper
from .errors import GuiError
from .shape import looks_like_connection_string

#: The statuses `IgnoreRule` accepts. Deliberately not `ObjectStatus`, which also has `match`.
RULE_STATUSES: tuple[str, ...] = ("missing_in_target", "extra_in_target", "differs")

#: The actions `IgnoreRule` accepts.
RULE_ACTIONS: tuple[str, ...] = ("ignore", "warn", "info")

_LIST_FIELDS = ("kinds", "names", "attributes", "targets", "statuses")
_EDITABLE = ("id", "reason", "action", *_LIST_FIELDS)
_RULE_KEY_ORDER = ("id", "reason", "kinds", "names", "attributes", "targets", "statuses", "action")


def rule_kinds() -> tuple[str, ...]:
    """Object kinds a rule may name, in report order."""
    return tuple(kind.value for kind in ObjectKind)


@dataclass
class IgnoresDocument:
    """An ignore ruleset being edited, plus whether it has unsaved changes."""

    path: Path | None
    config: IgnoreConfig
    dirty: bool = False

    @classmethod
    def for_config(cls, document: ConfigDocument) -> IgnoresDocument:
        try:
            resolved = resolve_ignores(document.config, document.path)
        except ConfigError as exc:
            raise GuiError(str(exc), field="ignores") from exc
        path: Path | None = None
        if resolved is not None and document.config.ignores_file is not None:
            base = document.path.parent if document.path is not None else Path()
            path = base / document.config.ignores_file
        return cls(path=path, config=resolved if resolved is not None else IgnoreConfig())

    # -- Reading --------------------------------------------------------------------------

    def rule_rows(self) -> list[dict[str, Any]]:
        return [_row(rule, read_only=False) for rule in self.config.rules]

    def default_rows(self) -> list[dict[str, Any]]:
        return [_row(rule, read_only=True) for rule in load_default_ignores().rules]

    # -- Editing --------------------------------------------------------------------------

    def update_rule(self, rule_id: str, field: str, value: Any) -> None:
        """Change one field of a rule.

        Not validated here: a form passes through half-typed states, and :meth:`validate` reports
        them against the rule.
        """
        if field not in _EDITABLE:
            raise GuiError("unknown rule field", field=field)
        index = self._index(rule_id)
        if field in _LIST_FIELDS:
            items = value.split(",") if isinstance(value, str) else (value or ())
            value = tuple(str(item).strip() for item in items if str(item).strip())
        elif field == "reason" and value == "":
            value = None
        rules = list(self.config.rules)
        rules[index] = rules[index].model_copy(update={field: value})
        self.config = self.config.model_copy(update={"rules": tuple(rules)})
        self.dirty = True

    def add_rule(self, rule_id: str) -> None:
        if not rule_id or not rule_id.strip():
            raise GuiError("a rule id must not be empty", field="id")
        # Shape first: the duplicate message names the id, which must never be a pasted DSN.
        if looks_like_connection_string(rule_id):
            raise GuiError(_SHAPE_MESSAGE, field="id")
        if any(rule.id == rule_id for rule in self.config.rules):
            raise GuiError(f"a rule with id {rule_id!r} already exists", field="id")
        # A rule restricting nothing would suppress the whole report; seed one that validates.
        rule = IgnoreRule(id=rule_id, names=("schema.name",))
        self.config = self.config.model_copy(update={"rules": (*self.config.rules, rule)})
        self.dirty = True

    def remove_rule(self, rule_id: str) -> None:
        index = self._index(rule_id)
        rules = tuple(r for i, r in enumerate(self.config.rules) if i != index)
        self.config = self.config.model_copy(update={"rules": rules})
        self.dirty = True

    def _index(self, rule_id: str) -> int:
        for index, rule in enumerate(self.config.rules):
            if rule.id == rule_id:
                return index
        raise GuiError("no rule with that id", field="id")

    # -- Validation and saving ------------------------------------------------------------

    def validate(self) -> list[GuiError]:
        return _problems(self.config)

    def to_yaml(self) -> str:
        self._require_valid()
        return self._render()

    def _require_valid(self) -> None:
        errors = _problems(self.config)
        if errors:
            summary = "; ".join(str(e) for e in errors)
            raise GuiError(f"cannot write an invalid ignore ruleset ({summary})", errors[0].field)

    def _render(self) -> str:
        data = self.config.model_dump(mode="json", exclude_unset=True)
        ordered: dict[str, Any] = {"version": data.pop("version", 1)}
        ordered.update(data)
        ordered["rules"] = [
            {
                k: rule[k]
                for k in (*_RULE_KEY_ORDER, *(k for k in rule if k not in _RULE_KEY_ORDER))
                if k in rule
            }
            for rule in ordered.get("rules", [])
        ]
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
        self._require_valid()
        text = self._render()
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


def _row(rule: IgnoreRule, *, read_only: bool) -> dict[str, Any]:
    return {
        "id": rule.id,
        "reason": rule.reason or "",
        "kinds": list(rule.kinds),
        "names": list(rule.names),
        "attributes": list(rule.attributes),
        "targets": list(rule.targets),
        "statuses": list(rule.statuses),
        "action": rule.action,
        "read_only": read_only,
    }


def _problems(config: IgnoreConfig) -> list[GuiError]:
    """Everything wrong with ``config``, including rules that could never match a finding."""
    errors: list[GuiError] = []
    known_kinds = set(rule_kinds())
    seen: set[str] = set()

    def report(index: int, field: str, message: str) -> None:
        errors.append(GuiError(f"rule {index + 1}, {field}: {message}", f"rules.{index}.{field}"))

    for index, rule in enumerate(config.rules):
        if rule.id in seen:
            report(index, "id", "duplicate rule id; the later rule would never be reached")
        seen.add(rule.id)
        if not rule.id.strip():
            report(index, "id", "a rule id must not be empty")
        if any(k not in known_kinds for k in rule.kinds):
            report(index, "kinds", "names a kind that does not exist, so the rule can never match")
        if any(s not in RULE_STATUSES for s in rule.statuses):
            report(
                index, "statuses", "names a status that is not valid, so the rule can never match"
            )
        if rule.action not in RULE_ACTIONS:
            report(index, "action", "is not one of ignore, warn or info, so the rule does nothing")
        if any(not n.strip() for n in rule.names):
            report(index, "names", "contains an empty pattern, which matches nothing")
        if any(not t.strip() for t in rule.targets):
            report(index, "targets", "contains an empty target label, which matches nothing")
        if rule.attributes:
            _check_attributes(rule, known_kinds, lambda f, m, i=index: report(i, f, m))

    for index, rule in enumerate(config.rules):
        for field, text in _strings(rule):
            if looks_like_connection_string(text):
                errors.append(
                    GuiError(
                        f"rule {index + 1}, {field.split('.')[0]}: {_SHAPE_MESSAGE}",
                        f"rules.{index}.{field}",
                    )
                )

    try:
        IgnoreConfig.model_validate(config.model_dump())
    except ValidationError as exc:
        for e in exc.errors():
            loc = tuple(e["loc"])
            # Duplicates, kinds and statuses are reported above; the model's own wording quotes
            # the value, which this module never does.
            if len(loc) < 2 or loc[0] != "rules" or loc[-1] in ("kinds", "statuses", "id"):
                continue
            if any(err.field == ".".join(str(p) for p in loc) for err in errors):
                continue
            message = str(e["msg"]).removeprefix("Value error, ")
            if e["type"] == "value_error":
                report(int(loc[1]), "rule" if len(loc) == 2 else str(loc[-1]), message)
            else:
                report(int(loc[1]), str(loc[-1]), "is not valid")
    return errors


_SHAPE_MESSAGE = (
    "a connection string does not belong here; the ignore rules hold names and labels only"
)


def _strings(rule: IgnoreRule) -> list[tuple[str, str]]:
    """Every string the rule would write, with the path of the field that holds it."""
    found: list[tuple[str, str]] = [("id", rule.id), ("action", str(rule.action))]
    if rule.reason:
        found.append(("reason", rule.reason))
    for name in _LIST_FIELDS:
        found.extend((f"{name}.{i}", str(v)) for i, v in enumerate(getattr(rule, name)))
    return found


def _check_attributes(rule: IgnoreRule, known_kinds: set[str], report: Any) -> None:
    """An attribute rule applies to differences only, and to the kind its attributes name."""
    if rule.statuses and "differs" not in rule.statuses:
        report(
            "statuses",
            "attributes only apply to differences, and statuses excludes differs, "
            "so the rule can never match",
        )
    prefixes = {a.split(".", 1)[0] for a in rule.attributes}
    if any("." not in a or not a.split(".", 1)[1] for a in rule.attributes):
        report("attributes", "must be written kind.attribute, so the rule can never match")
    elif not prefixes <= known_kinds:
        report("attributes", "names a kind that does not exist, so the rule can never match")
    elif rule.kinds and not prefixes & set(rule.kinds):
        report(
            "attributes", "names only kinds that the rule's kinds exclude, so it can never match"
        )
