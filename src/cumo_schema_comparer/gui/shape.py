"""The shape check for a pasted connection string, shared by every editor of a committed file.

Detection is by shape only (a URL, or libpq ``keyword=value`` pairs); ``secrets.py`` scrubs known
secrets from output, which is a different job.
"""

from __future__ import annotations

import re

#: A libpq keyword/value pair.
KEYWORD_PAIR = re.compile(
    r"(?<![\w-])(?:host|hostaddr|port|dbname|user|password|passfile|sslmode|service)\s*=",
    re.IGNORECASE,
)


def looks_like_connection_string(value: str) -> bool:
    return "://" in value or KEYWORD_PAIR.search(value) is not None
