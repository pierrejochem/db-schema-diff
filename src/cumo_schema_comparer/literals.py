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
_USERINFO_URL = re.compile(r"[a-z][a-z0-9+.-]*://[^/?#\s]*@", re.IGNORECASE)
_SECRET_KEYWORD = re.compile(
    r"(?<![\w-])(?:password|passfile|sslpassword|sslkey)\s*=", re.IGNORECASE
)


def looks_like_connection_string(value: str) -> bool:
    return bool(
        _POSTGRES_URL.search(value) or _USERINFO_URL.search(value) or _SECRET_KEYWORD.search(value)
    )
