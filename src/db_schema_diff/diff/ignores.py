"""Applying the ignore ruleset.

A drift detector with no way to say "yes, we know" is abandoned after its first real run. But a
suppression that makes a finding vanish without trace is worse than no suppression at all, so
everything here keeps the evidence:

* an ignored finding still appears in the JSON report and as a skipped case in JUnit, tagged with
  the id of the rule that suppressed it;
* ``warn`` and ``info`` clamp a severity rather than hiding the finding, which is usually what
  somebody actually wants — keep it visible, stop failing the build over it.

Rules come in two scopes, and the distinction is the useful part. A rule naming ``attributes``
applies to individual differences, so it can say "a different collation is fine" without also
saying "a missing column is fine". A rule without ``attributes`` applies to whole objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase
from functools import lru_cache
from importlib import resources
from typing import Self

import yaml

from ..config.model import IgnoreConfig, IgnoreRule
from ..model.keys import ObjectKey
from ..model.kinds import ObjectKind
from .model import ObjectStatus
from .severity import Severity

#: The bundled ruleset, applied unless ``--no-default-ignores`` is passed.
DEFAULT_IGNORES_RESOURCE = "default_ignores.yaml"


class Action(StrEnum):
    """What a rule does to a finding."""

    KEEP = "keep"
    """No rule matched."""
    IGNORE = "ignore"
    WARN = "warn"
    INFO = "info"

    @property
    def clamps_to(self) -> Severity | None:
        """The severity ceiling this action imposes, if any."""
        if self is Action.WARN:
            return Severity.WARNING
        if self is Action.INFO:
            return Severity.INFO
        return None


@dataclass(frozen=True, slots=True)
class Decision:
    """What to do with one finding or one attribute difference."""

    action: Action = Action.KEEP
    rule_id: str | None = None

    @property
    def suppressed(self) -> bool:
        return self.action is Action.IGNORE

    @property
    def clamped(self) -> bool:
        return self.action in (Action.WARN, Action.INFO)


KEEP = Decision()


class IgnoreRuleSet:
    """The rules for one run, ready to be asked about a finding."""

    __slots__ = ("_attribute_rules", "_case_insensitive", "_object_rules")

    def __init__(self, config: IgnoreConfig) -> None:
        self._case_insensitive = config.options.case_insensitive_globs
        # Split by scope once, so a lookup only walks the rules that could possibly apply.
        self._attribute_rules = tuple(r for r in config.rules if r.attributes)
        self._object_rules = tuple(r for r in config.rules if not r.attributes)

    @classmethod
    def from_config(
        cls, config: IgnoreConfig | None, *, defaults: IgnoreConfig | None = None
    ) -> Self:
        """Build a ruleset from a project's rules layered over the defaults."""
        base = defaults or IgnoreConfig()
        merged = base.merged_with(config) if config is not None else base
        return cls(merged)

    @classmethod
    def empty(cls) -> Self:
        """A ruleset that suppresses nothing."""
        return cls(IgnoreConfig())

    def __len__(self) -> int:
        return len(self._object_rules) + len(self._attribute_rules)

    @property
    def rule_ids(self) -> tuple[str, ...]:
        """Every rule's id, for reporting which ruleset was in force."""
        return tuple(r.id for r in (*self._object_rules, *self._attribute_rules))

    def for_object(self, *, target: str, key: ObjectKey, status: ObjectStatus) -> Decision:
        """What to do with a whole finding."""
        for rule in self._object_rules:
            if self._matches(rule, target=target, key=key, status=status, attribute=None):
                return Decision(Action(rule.action), rule.id)
        return KEEP

    def for_attribute(
        self, *, target: str, key: ObjectKey, status: ObjectStatus, attribute: str
    ) -> Decision:
        """What to do with one attribute difference."""
        for rule in self._attribute_rules:
            if self._matches(rule, target=target, key=key, status=status, attribute=attribute):
                return Decision(Action(rule.action), rule.id)
        return KEEP

    # -- Matching -------------------------------------------------------------------------

    def _matches(
        self,
        rule: IgnoreRule,
        *,
        target: str,
        key: ObjectKey,
        status: ObjectStatus,
        attribute: str | None,
    ) -> bool:
        if rule.targets and target not in rule.targets:
            return False
        if rule.kinds and key.kind.value not in rule.kinds:
            return False
        if rule.statuses and status.value not in rule.statuses:
            return False
        if rule.attributes and (attribute is None or attribute not in rule.attributes):
            return False
        return not rule.names or self._name_matches(rule.names, key)

    def _name_matches(self, patterns: tuple[str, ...], key: ObjectKey) -> bool:
        """Whether any glob matches this object.

        Both ``schema.name`` and ``schema.name.subname`` are tried, so ``quartz.*`` catches a
        Quartz table and ``*.qrtz_*`` catches its columns without the author writing two rules.
        """
        candidates = (key.qualified, key.path)
        for pattern in patterns:
            for candidate in candidates:
                if _glob(pattern, candidate, self._case_insensitive):
                    return True
        return False


@lru_cache(maxsize=4096)
def _glob(pattern: str, candidate: str, case_insensitive: bool) -> bool:
    """Match one glob, memoised because the same pairs recur across thousands of findings.

    ``fnmatchcase`` rather than ``fnmatch``: the latter folds case according to the *operating
    system*, which would make the same ruleset behave differently on macOS and Linux.
    """
    if case_insensitive:
        return fnmatchcase(candidate.lower(), pattern.lower())
    return fnmatchcase(candidate, pattern)


def load_default_ignores() -> IgnoreConfig:
    """The bundled ruleset.

    Read through ``importlib.resources`` so it is found identically from a source checkout and from
    an installed wheel.
    """
    text = (
        resources.files("db_schema_diff.resources")
        .joinpath(DEFAULT_IGNORES_RESOURCE)
        .read_text(encoding="utf-8")
    )
    return IgnoreConfig.model_validate(yaml.safe_load(text))


def kinds_of(*kinds: ObjectKind) -> tuple[str, ...]:
    """Helper for building rules in code, used by the tests."""
    return tuple(k.value for k in kinds)
