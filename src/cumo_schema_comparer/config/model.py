"""Validated configuration.

Every model sets ``extra="forbid"``. This file is hand-edited, so a typo'd key must be an
error rather than a silently ignored default: ``exclude_schema`` instead of
``exclude_schemas`` would otherwise compare the very schemas the user meant to skip.

The config is deliberately non-secret and safe to commit. It names environment *variables*;
resolving them is :meth:`ComparerConfig.resolve_credentials`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..errors import ConfigError
from .secrets import Dsn, resolve_all

FailOn = Literal["error", "warning", "any", "never"]

#: Schemas excluded unless the user opts in: Liquibase bookkeeping and Quartz runtime state.
STRICT = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class LiquibaseRef(BaseModel):
    """Explicit location of a ``DATABASECHANGELOG`` table.

    Only needed to disambiguate: the table is normally located by scanning the catalog,
    because it is not reliably in ``public`` (``cumo-invoicing`` keeps it in a hyphenated
    schema of its own).
    """

    model_config = ConfigDict(
        extra="forbid", frozen=True, str_strip_whitespace=True, populate_by_name=True
    )

    # The YAML key is `schema`; the attribute is `schema_name` because `BaseModel.schema`
    # is a pydantic method and shadowing it is a trap for every later reader.
    schema_name: str = Field(alias="schema", min_length=1)
    table: str = Field(default="DATABASECHANGELOG", min_length=1)


class SourceRef(BaseModel):
    """One database to inspect: the master, or one target environment."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, str_strip_whitespace=True, populate_by_name=True
    )

    label: str = Field(min_length=1)
    """Short name used in reports and in ``--target``. Must be unique within a config."""

    dsn_env: str = Field(min_length=1)
    """Name of the environment variable holding this source's libpq connection string."""

    host: str | None = None
    """Documentation only. Connections always use the DSN, never this."""

    database: str | None = None
    """Documentation only."""

    schemas: tuple[str, ...] | None = None
    """Explicit schema allowlist. ``None`` means every non-system schema."""

    schema_map: dict[str, str] = Field(default_factory=dict)
    """Master-schema-name to this-source-schema-name renames.

    Needed because services such as ``cumo-bpf`` deploy with a per-environment
    ``--defaultSchemaName``, so the same table lives under a different schema name per
    environment.
    """

    liquibase: LiquibaseRef | None = None

    @field_validator("label")
    @classmethod
    def _label_has_no_whitespace(cls, value: str) -> str:
        if any(c.isspace() for c in value):
            raise ValueError("label must not contain whitespace")
        return value


class IgnoreRule(BaseModel):
    """One rule that suppresses or downgrades findings.

    Every dimension left unset means "any", so a rule is as broad or as narrow as it needs to be.
    An empty rule would match everything, which is refused.
    """

    model_config = STRICT

    id: str = Field(min_length=1)
    """Recorded on every finding the rule touches, so a suppression stays traceable to its cause."""

    reason: str | None = None
    """Why this is acceptable. Optional to the parser, expected by a reviewer."""

    kinds: tuple[str, ...] = ()
    """Object kinds this applies to. Empty means any."""

    names: tuple[str, ...] = ()
    """Globs matched against ``schema.name`` and ``schema.name.subname``. Empty means any."""

    attributes: tuple[str, ...] = ()
    """Dotted ``kind.attribute`` names, e.g. ``column.collation``.

    A rule with attributes set applies to individual differences; one without applies to whole
    objects. That is what lets a rule say "tolerate a different collation" without also saying
    "tolerate a missing column".
    """

    targets: tuple[str, ...] = ()
    """Target labels. Empty means every target."""

    statuses: tuple[str, ...] = ()
    """``missing_in_target``, ``extra_in_target`` or ``differs``. Empty means any.

    This is what lets a rule tolerate an *extra* object in a development environment while still
    failing on a *missing* one, which is usually what people actually want.
    """

    action: Literal["ignore", "warn", "info"] = "ignore"
    """``ignore`` suppresses the finding — auditably, it still appears in the JSON report and as a
    skipped case in JUnit. ``warn`` and ``info`` clamp its severity instead, which keeps it visible
    without failing the build."""

    @model_validator(mode="after")
    def _rule_is_not_unbounded(self) -> IgnoreRule:
        if not (self.kinds or self.names or self.attributes or self.statuses):
            raise ValueError(
                "a rule must restrict at least one of kinds, names, attributes or statuses; "
                "as written it would match every finding"
            )
        return self

    @field_validator("kinds")
    @classmethod
    def _kinds_are_known(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        from ..model.kinds import ObjectKind

        known = {k.value for k in ObjectKind}
        unknown = [k for k in value if k not in known]
        if unknown:
            raise ValueError(
                f"unknown object kind(s) {', '.join(unknown)}; known kinds are "
                + ", ".join(sorted(known))
            )
        return value

    @field_validator("statuses")
    @classmethod
    def _statuses_are_known(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        known = {"missing_in_target", "extra_in_target", "differs"}
        unknown = [s for s in value if s not in known]
        if unknown:
            raise ValueError(
                f"unknown status(es) {', '.join(unknown)}; known statuses are "
                + ", ".join(sorted(known))
            )
        return value


class IgnoreOptions(BaseModel):
    """Settings for the ignore ruleset as a whole."""

    model_config = STRICT

    case_insensitive_globs: bool = True
    """Match name globs regardless of case.

    PostgreSQL identifiers *are* case-sensitive, but an ignore rule is written by a person who
    should not have to know whether Liquibase wrote ``QRTZ_LOCKS`` or ``qrtz_locks`` on this
    particular database.
    """


class IgnoreConfig(BaseModel):
    """A ruleset, from its own file or inline in the main config."""

    model_config = STRICT

    version: Literal[1] = 1
    options: IgnoreOptions = Field(default_factory=IgnoreOptions)
    rules: tuple[IgnoreRule, ...] = ()

    @model_validator(mode="after")
    def _rule_ids_are_unique(self) -> IgnoreConfig:
        seen: set[str] = set()
        for rule in self.rules:
            if rule.id in seen:
                raise ValueError(f"duplicate ignore rule id {rule.id!r}")
            seen.add(rule.id)
        return self

    def merged_with(self, other: IgnoreConfig) -> IgnoreConfig:
        """This ruleset plus ``other``'s rules, with ``other`` winning on a shared id.

        Used to layer a project's rules over the bundled defaults, so a project can override one
        default without restating the rest.
        """
        by_id = {rule.id: rule for rule in self.rules}
        by_id.update({rule.id: rule for rule in other.rules})
        return IgnoreConfig(version=1, options=other.options, rules=tuple(by_id.values()))


class Options(BaseModel):
    """Run-time knobs. Every one is also settable on the command line."""

    model_config = STRICT

    connect_timeout_seconds: int = Field(default=10, ge=1, le=300)
    statement_timeout_seconds: int = Field(default=60, ge=1, le=3600)
    lock_timeout_seconds: int = Field(default=3, ge=1, le=600)
    parallel: bool = True
    max_workers: int = Field(default=4, ge=1, le=32)
    fail_on: FailOn = "error"
    ignore_column_order: bool = False
    include_owners: bool = False
    include_comments: bool = False
    include_grants: bool = False


class ComparerConfig(BaseModel):
    """One master database and the targets it is compared against.

    Scoped to a single database on purpose: the CUMO server hosts ~18 of them, one per
    service, so the unit of comparison is a (server, database) pair and each gets its own
    config file.
    """

    model_config = STRICT

    version: Literal[1]
    name: str = Field(min_length=1)
    master: SourceRef
    targets: tuple[SourceRef, ...] = Field(min_length=1)
    exclude_schemas: tuple[str, ...] = ()
    ignores_file: str | None = None
    """Path to an ignore ruleset, relative to this config file."""
    ignores: IgnoreConfig | None = None
    """An inline ruleset, for a project small enough not to want a second file."""
    options: Options = Field(default_factory=Options)

    @model_validator(mode="after")
    def _labels_are_unique(self) -> ComparerConfig:
        seen = {self.master.label}
        for target in self.targets:
            if target.label in seen:
                raise ValueError(
                    f"duplicate source label {target.label!r}: "
                    "every label must be unique within a config, including the master's"
                )
            seen.add(target.label)
        return self

    @property
    def sources(self) -> tuple[SourceRef, ...]:
        """Master first, then the targets in declaration order."""
        return (self.master, *self.targets)

    def target(self, label: str) -> SourceRef:
        """Look up one target by label.

        Raises :class:`ConfigError`, not ``KeyError``: a mistyped ``--target`` is bad input, and
        should produce the documented exit code and a message naming the valid labels rather than
        a traceback.
        """
        for candidate in self.targets:
            if candidate.label == label:
                return candidate
        known = ", ".join(t.label for t in self.targets)
        raise ConfigError(f"unknown target {label!r}; configured targets are: {known}")

    def role_of(self, source: SourceRef) -> str:
        """Human description used in credential failure messages."""
        kind = "master" if source is self.master or source == self.master else "target"
        return f"{kind} {source.label!r}"

    def warnings(self) -> tuple[str, ...]:
        """Non-fatal oddities worth telling the user about."""
        found: list[str] = []
        master_env = self.master.dsn_env
        for target in self.targets:
            if target.dsn_env == master_env:
                found.append(
                    f"target {target.label!r} reads the same credential as the master "
                    f"(${master_env}); it will be compared against itself"
                )
        return tuple(found)

    def resolve_credentials(self) -> dict[str, Dsn]:
        """Resolve every source's DSN from the environment, keyed by source label.

        Every missing variable is reported in a single failure, so a three-variable mistake
        costs one run rather than three.
        """
        requests = [(source.dsn_env, self.role_of(source)) for source in self.sources]
        by_env = resolve_all(requests)
        return {source.label: by_env[source.dsn_env] for source in self.sources}
