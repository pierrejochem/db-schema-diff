"""Masking credential-shaped string literals in definition text, keeping the SQL around them.

Lives in ``normalize`` because it needs the tokenizer, and ``literals`` must stay stdlib-only
(the GUI re-exports from it).

The tokenizer, not a regex, decides where a literal begins and ends: a routine body is
dollar-quoted, and an apostrophe in a comment inside it (``don't``) would shift any
quote-pairing regex and leave a connection string unmasked. Each literal's whole body is judged
by the predicate and replaced whole, so a secret is never half-replaced.
"""

from __future__ import annotations

import hashlib

from cumo_schema_comparer.literals import looks_like_connection_string
from cumo_schema_comparer.normalize.tokenizer import (
    _DOLLAR_TAG,
    Token,
    TokenType,
    _scan_single_quoted,
    tokenize,
)

MASK_PREFIX = "***:"

#: Dollar-quote nesting we are willing to recurse through; deeper bodies are judged as one blob.
_MAX_DEPTH = 32


def _mask_for(secret: str) -> str:
    """A stable, non-reversible stand-in that still differs between different secrets.

    No per-run or per-host salt: master and target must produce the same mask for the same
    literal, or an unchanged credential would report as drift on every run. 12 hex digits is
    48 bits.
    """
    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]
    return f"{MASK_PREFIX}{digest}"


def _mask_quoted(text: str) -> str:
    """Mask one single-quoted literal token (``'..'``, ``E'..'``, ``U&'..'``)."""
    quote = text.index("'")
    prefix = text[:quote]
    backslashes = prefix[:1] in ("e", "E")
    # The tokenizer treats an unterminated literal as running to the end of input. Appending a
    # non-quote character tells the two cases apart: a terminated literal still ends where it did,
    # an unterminated one swallows the extra character.
    terminated = _scan_single_quoted(text + "x", quote, backslash_escapes=backslashes) == len(text)
    body = text[quote + 1 : -1] if terminated else text[quote + 1 :]
    if not looks_like_connection_string(body):
        return text
    return f"{prefix}'{_mask_for(body)}" + ("'" if terminated else "")


def _mask_quoted_or_whole(text: str) -> str:
    return _mask_for(text) if looks_like_connection_string(text) else text


def _mask_dollar(text: str, depth: int) -> str:
    tag_match = _DOLLAR_TAG.match(text)
    if tag_match is None:  # unreachable: the tokenizer only emits `$` strings that open a tag
        return _mask_quoted_or_whole(text)
    tag = tag_match.group(0)
    terminated = len(text) >= 2 * len(tag) and text.endswith(tag)
    inner = text[len(tag) : -len(tag)] if terminated else text[len(tag) :]
    closing = tag if terminated else ""
    if depth >= _MAX_DEPTH:
        masked = _mask_for(inner) if looks_like_connection_string(inner) else inner
        return f"{tag}{masked}{closing}"
    tokens = tokenize(inner)
    if not any(token.type is TokenType.STRING for token in tokens):
        # A plain dollar-quoted string value rather than a body holding literals.
        if looks_like_connection_string(inner):
            return f"{tag}{_mask_for(inner)}{closing}"
        return text
    return f"{tag}{_mask_tokens(inner, tokens, depth + 1)}{closing}"


def _mask_token(token: Token, depth: int) -> str:
    if token.text.startswith("$"):
        return _mask_dollar(token.text, depth)
    return _mask_quoted(token.text)


def _mask_tokens(text: str, tokens: list[Token], depth: int) -> str:
    pieces: list[str] = []
    cursor = 0
    for token in tokens:
        if token.type is not TokenType.STRING:
            continue
        pieces.append(text[cursor : token.start])
        pieces.append(_mask_token(token, depth))
        cursor = token.start + len(token.text)
    pieces.append(text[cursor:])
    return "".join(pieces)


def mask_literals(text: str | None) -> str | None:
    """Replace credential-shaped string literals whole, keeping the surrounding SQL intact.

    Inside a dollar-quoted body the inner literals are masked, not the body. A masked literal no
    longer looks like a connection string, so applying this twice equals applying it once; there
    is deliberately no "starts with the mask prefix" skip, which would let
    ``'***:postgresql://u:pw@h'`` through.

    An unterminated literal runs to the end of the input (the tokenizer's rule), so it is judged
    and masked as a whole, and left unterminated.
    """
    if text is None:
        return None
    return _mask_tokens(text, tokenize(text), 0)
