"""Canonicalising a routine body.

The body is compared as a hash of its canonical text, and that text comes from ``prosrc`` rather
than ``pg_get_functiondef``. That function re-prints the whole CREATE statement, and its header
formatting changes between PostgreSQL releases, so comparing it reports every routine in the
database as changed the moment two environments run different majors.

What is normalised away is layout, and only layout: line endings, indentation, blank lines and
comments. Migrations authored on Windows are common in this platform, and a line ending is not a
behaviour difference.

Two things are deliberately **not** normalised, because doing so would let genuinely different
routines compare equal:

* **Case.** A plpgsql body is full of string literals, and lower-casing
  ``RAISE EXCEPTION 'Invoice % not found'`` would change the message the function produces.
* **Whitespace inside a string literal.** ``RETURN 'a  b'`` and ``RETURN 'a b'`` return different
  strings, which is why the body is tokenised rather than processed line by line.
"""

from __future__ import annotations

import hashlib

from .tokenizer import tokenize


def canonical_body(raw: str | None) -> str | None:
    """Canonical form of a routine body, or ``None`` when there is none.

    Tokenised rather than processed line by line, so that whitespace *inside* a string literal is
    preserved. A naive collapse would make ``RETURN 'a  b'`` and ``RETURN 'a b'`` canonicalise
    identically, and two functions returning different strings would compare equal — a false
    negative, which is the failure mode this tool must not have.

    Layout is discarded entirely: the tokens are rejoined with single spaces, so indentation,
    line breaks and blank lines cannot make two identical routines differ. Comments go too — a
    comment does not change what a function does.

    An aggregate or window function has no extractable body and yields ``None``; those are compared
    on their declared attributes alone.
    """
    if raw is None:
        return None
    tokens = tokenize(raw.replace("\r\n", "\n").replace("\r", "\n"))
    if not tokens:
        return ""
    # Literals and quoted identifiers keep their exact text; everything else keeps its own spelling
    # but loses the whitespace around it. Case is never folded: a plpgsql body is full of string
    # literals, and lower-casing `RAISE EXCEPTION 'Invoice % not found'` would change what it says.
    return " ".join(token.text for token in tokens)


def body_hash(raw: str | None) -> str | None:
    """SHA-256 of the canonical body.

    A hash rather than the text: bodies run to hundreds of lines, and a report needs to say *that*
    they differ far more often than it needs to show how. The raw text is kept for display.
    """
    canonical = canonical_body(raw)
    if canonical is None:
        return None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def bodies_equivalent(left: str | None, right: str | None) -> bool:
    """Whether two routine bodies do the same thing, ignoring layout."""
    return canonical_body(left) == canonical_body(right)
