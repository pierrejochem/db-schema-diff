"""Canonical type names.

The input is always ``format_type(atttypid, atttypmod)`` — one source of truth. Rebuilding a
type from ``information_schema``'s ``data_type`` plus ``character_maximum_length``,
``numeric_precision`` and ``numeric_scale`` is the classic way to get this wrong, because the
four columns disagree about defaults.

Two rules govern everything here:

* Collapse **spelling**: ``character varying(50)`` and ``varchar(50)`` are the same type.
* Never collapse **meaning**: ``numeric`` is not ``numeric(10,2)``, and ``varchar`` is not
  ``varchar(255)``. Treating a missing type modifier as "any modifier" would hide the most
  common real column change there is.
"""

from __future__ import annotations

import re

#: Long spelling to canonical short spelling. Only built-in types appear here; a user type is
#: whatever the catalog called it.
_ALIASES: dict[str, str] = {
    "character varying": "varchar",
    "character": "bpchar",
    "char": "bpchar",
    "bit varying": "varbit",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamptz",
    "time without time zone": "time",
    "time with time zone": "timetz",
    "double precision": "float8",
    "real": "float4",
    "integer": "int4",
    "int": "int4",
    "smallint": "int2",
    "bigint": "int8",
    "boolean": "bool",
    "decimal": "numeric",
    "national character varying": "varchar",
    "national character": "bpchar",
}

#: ``timestamp(3) without time zone`` puts the modifier in the middle of the name.
_INFIX_ALIASES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"^timestamp\s*\((\d+)\)\s*without\s+time\s+zone$", re.IGNORECASE),
        r"timestamp(\1)",
    ),
    (
        re.compile(r"^timestamp\s*\((\d+)\)\s*with\s+time\s+zone$", re.IGNORECASE),
        r"timestamptz(\1)",
    ),
    (re.compile(r"^time\s*\((\d+)\)\s*without\s+time\s+zone$", re.IGNORECASE), r"time(\1)"),
    (re.compile(r"^time\s*\((\d+)\)\s*with\s+time\s+zone$", re.IGNORECASE), r"timetz(\1)"),
    (re.compile(r"^interval\s+(\w[\w\s]*?)\s*\((\d+)\)$", re.IGNORECASE), r"interval \1(\2)"),
)

_SPLIT = re.compile(r"^(?P<base>.*?)\s*(?:\((?P<mod>[^()]*)\))?\s*(?P<array>(?:\[\s*\d*\s*\])*)$")
_WHITESPACE = re.compile(r"\s+")


def canonical_type(raw: str | None) -> str | None:
    """Canonical spelling of one type name.

    ``None`` and ``""`` pass through so callers need no special case for an absent type.
    """
    if raw is None or not raw.strip():
        return raw
    text = _WHITESPACE.sub(" ", raw.strip())

    for pattern, replacement in _INFIX_ALIASES:
        if pattern.match(text):
            text = pattern.sub(replacement, text)
            break

    match = _SPLIT.match(text)
    if match is None:  # pragma: no cover - the pattern matches any string
        return text
    base = match.group("base").strip()
    modifier = match.group("mod")
    array = match.group("array") or ""

    base = _canonical_base(base)
    modifier = _canonical_modifier(modifier)

    # Declared array dimensions are not enforced by PostgreSQL, so `int4[][]` and `int4[]`
    # describe exactly the same column. Collapse to a single pair of brackets.
    suffix = "[]" if array else ""
    return f"{base}{modifier}{suffix}"


def _canonical_base(base: str) -> str:
    """Alias a built-in base name; leave a user or qualified name untouched."""
    # A qualified or quoted name is a user type. Case and hyphens are significant there:
    # "cumo-invoicing" is a real schema name in this platform.
    if "." in base or '"' in base:
        return base
    return _ALIASES.get(base.lower(), base.lower())


def _canonical_modifier(modifier: str | None) -> str:
    """Normalise a type modifier, preserving whether one was specified at all."""
    if modifier is None:
        return ""
    parts = [part.strip() for part in modifier.split(",") if part.strip()]
    if not parts:
        return ""
    # numeric(10,0) and numeric(10) mean the same thing; a non-zero scale does not.
    if len(parts) == 2 and parts[1] == "0":
        parts = parts[:1]
    return "(" + ",".join(parts) + ")"


def split_type(raw: str | None) -> tuple[str, str]:
    """Canonical type split into its base name and its modifier.

    ``varchar(50)`` becomes ``("varchar", "(50)")``; ``numeric`` becomes ``("numeric", "")``.
    An empty modifier means none was specified, which is information in its own right.
    """
    canonical = canonical_type(raw) or ""
    if canonical.endswith("[]"):
        base, modifier = split_type(canonical[:-2])
        return base + "[]", modifier
    if canonical.endswith(")") and "(" in canonical:
        index = canonical.index("(")
        return canonical[:index], canonical[index:]
    return canonical, ""


def cast_is_redundant(cast_type: str | None, column_type: str | None) -> bool:
    """Whether a ``::cast_type`` on a column of ``column_type`` carries no information.

    The server re-prints a column default with a cast to the column's own type, which says
    nothing: ``'foo'::character varying`` on a ``varchar(50)`` column behaves exactly like
    ``'foo'``. Two cases are *not* redundant and must survive:

    * a different base type — ``'foo'::text`` on a ``varchar`` column is a real difference;
    * a narrower modifier — ``x::varchar(10)`` on a ``varchar(50)`` column truncates.
    """
    if cast_type is None or column_type is None:
        return False
    cast_base, cast_modifier = split_type(cast_type)
    column_base, column_modifier = split_type(column_type)
    if not cast_base or cast_base != column_base:
        return False
    return cast_modifier in ("", column_modifier)


def types_equivalent(left: str | None, right: str | None) -> bool:
    """Whether two type spellings describe the same type."""
    return canonical_type(left) == canonical_type(right)
