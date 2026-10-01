"""The shape check for a pasted credential, shared by every editor of a committed file.

Only credential-bearing shapes are refused: a PostgreSQL URL, any URL with userinfo, or a libpq
keyword that carries a secret. A plain link or ``host=``/``user=`` text carries none, and refusing
them would only push people to delete useful context such as a ticket link.

This module began life as ``gui/shape.py`` and moved into the library, unchanged, because build-time
code needs it too and no library module may import from ``gui``.
"""

from __future__ import annotations

import hashlib
import re

_POSTGRES_URL = re.compile(r"postgres(?:ql)?://", re.IGNORECASE)
#: ``scheme://`` whose authority (up to the next ``/``, ``?`` or ``#``) contains ``@``.
#: The scheme is anchored at the start of its run of scheme characters, so the engine tries each
#: run once instead of once per letter in it; leading digits and punctuation are consumed before
#: the first letter, which keeps the verdict identical to an unanchored ``[a-z][a-z0-9+.-]*``.
_USERINFO_URL = re.compile(
    r"(?<![a-z0-9+.-])[0-9+.-]*[a-z][a-z0-9+.-]*://[^/?#\s]*@", re.IGNORECASE
)
_SECRET_KEYWORD = re.compile(
    r"(?<![\w-])(?:password|passfile|sslpassword|sslkey)\s*=", re.IGNORECASE
)


def looks_like_connection_string(value: str) -> bool:
    # ``in`` is one linear scan and false for nearly every input, including any long unbroken blob;
    # the scheme regex only runs when a ``://`` is actually there.
    return bool(
        _POSTGRES_URL.search(value)
        or ("://" in value and _USERINFO_URL.search(value))
        or _SECRET_KEYWORD.search(value)
    )


MASK_PREFIX = "***:"

#: A single-quoted SQL literal, honouring doubled-quote escaping. The alternatives are disjoint,
#: so matching is linear; the quoting tells us where a literal ends, so the whole body is judged.
_SQL_LITERAL = re.compile(r"'(?:''|[^'])*'")


def _mask_for(secret: str) -> str:
    """A stable, non-reversible stand-in that still differs between different secrets.

    No per-run or per-host salt: master and target must produce the same mask for the same
    literal, or an unchanged credential would report as drift on every run.
    """
    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()[:8]
    return f"{MASK_PREFIX}{digest}"


def mask_literals(text: str | None) -> str | None:
    """Replace credential-shaped string literals whole, keeping the surrounding SQL intact.

    A masked literal no longer looks like a connection string, so applying this twice is the same
    as applying it once. There is deliberately no "starts with the mask prefix" skip: that would
    let ``'***:postgresql://u:pw@h'`` through unmasked.
    """
    if text is None:
        return None

    def replace(match: re.Match[str]) -> str:
        body = match.group(0)[1:-1]
        if not looks_like_connection_string(body):
            return match.group(0)
        return f"'{_mask_for(body)}'"

    return _SQL_LITERAL.sub(replace, text)
