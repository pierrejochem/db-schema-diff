"""Column default canonicalization.

Defaults are the noisiest thing in a schema comparison, for two reasons.

The server re-prints them from the parse tree, adding parentheses and casts the author never
wrote. That part is handled by :mod:`db_schema_comparer.normalize.expressions`.

The second reason is specific to ``serial``. ``id serial`` is not a type: it materialises as
``integer NOT NULL DEFAULT nextval('t_id_seq'::regclass)`` plus an ownership link. The
sequence's *name* is generated, and it diverges between environments — a table rebuilt or
renamed at some point in one environment's history leaves ``t_id_seq1`` behind. Comparing the
literal default then reports drift on every serial column in the database while the schemas
are, in every way that matters, identical. So an owned-sequence default canonicalises to a
sentinel, and the real sequence name is reported separately, for information only.
"""

from __future__ import annotations

import re

from ..model.objects import SERIAL_SENTINEL
from .expressions import canonical_expr
from .identifiers import unquote
from .tokenizer import TokenType, tokenize

_NEXTVAL = re.compile(r"^\s*nextval\s*\(", re.IGNORECASE)


def owned_sequence_name(default: str | None) -> str | None:
    """The bare sequence name in a ``nextval(...)`` default, if it is one.

    The schema qualifier is stripped: the caller compares against the sequence PostgreSQL
    records as owned by the column, which is identified by name within its own schema.
    """
    if not default or not _NEXTVAL.match(default):
        return None
    tokens = tokenize(default)
    for token in tokens:
        if token.type is not TokenType.STRING:
            continue
        body = token.text[1:-1] if token.text.startswith("'") else token.text
        inner = tokenize(body)
        if not inner:
            return None
        # 'schema.seq' or just 'seq'. The last identifier is the sequence.
        names = [t.text for t in inner if t.is_quotable]
        return unquote(names[-1]) if names else None
    return None


def canonical_default(
    raw: str | None,
    *,
    column_type: str | None = None,
    owned_sequence: str | None = None,
) -> str | None:
    """Canonical form of one column default.

    ``owned_sequence`` is the sequence PostgreSQL records this column as owning, from
    ``pg_depend`` with ``deptype='a'``. When the default is a ``nextval`` on precisely that
    sequence, the result is :data:`SERIAL_SENTINEL`. A ``nextval`` on any *other* sequence is
    kept verbatim: sharing a sequence between columns is a deliberate design choice, and which
    sequence it is matters.
    """
    if raw is None:
        return None

    if owned_sequence is not None and owned_sequence_name(raw) == owned_sequence:
        return SERIAL_SENTINEL

    return canonical_expr(raw, column_type=column_type)
