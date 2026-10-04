"""The shape check for a pasted credential, shared by every editor of a committed file.

Only credential-bearing shapes are refused: a PostgreSQL URL, any URL with userinfo, or a libpq
keyword that carries a secret. A plain link or ``host=``/``user=`` text carries none, and refusing
them would only push people to delete useful context such as a ticket link.

This module began life as ``gui/shape.py`` and moved into the library, unchanged, because build-time
code needs it too and no library module may import from ``gui``.
"""

from __future__ import annotations

import re

_POSTGRES_URL = re.compile(r"postgres(?:ql)?://", re.IGNORECASE)
#: ``scheme://`` whose authority (up to the next ``/``, ``?`` or ``#``) contains ``@``.
#: The scheme is anchored at the start of its run of scheme characters, so the engine tries each
#: run once instead of once per letter in it; leading digits and punctuation are consumed before
#: the first letter, which keeps the verdict identical to an unanchored ``[a-z][a-z0-9+.-]*``.
_USERINFO_URL = re.compile(
    r"(?<![a-z0-9+.-])[0-9+.-]*[a-z][a-z0-9+.-]*://[^/?#\s]*@", re.IGNORECASE
)
#: The libpq keywords that carry a secret. The single source: the regex below is built from it and
#: ``normalize.redact`` masks the literal that follows any of them.
SECRET_KEYWORDS = ("password", "passfile", "sslpassword", "sslkey")
_SECRET_KEYWORD = re.compile(r"(?<![\w-])(?:" + "|".join(SECRET_KEYWORDS) + r")\s*=", re.IGNORECASE)


def looks_like_credential_url(value: str) -> bool:
    """Whether ``value`` holds a PostgreSQL URL or a URL with userinfo (no keyword forms)."""
    return bool(_POSTGRES_URL.search(value) or ("://" in value and _USERINFO_URL.search(value)))


def looks_like_connection_string(value: str) -> bool:
    # ``in`` is one linear scan and false for nearly every input, including any long unbroken blob;
    # the scheme regex only runs when a ``://`` is actually there.
    return looks_like_credential_url(value) or bool(_SECRET_KEYWORD.search(value))
