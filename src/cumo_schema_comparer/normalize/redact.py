"""Masking credentials in definition text (literals and comments), keeping the SQL around them.

Lives in ``normalize`` because it needs the tokenizer, and ``literals`` must stay stdlib-only
(the GUI re-exports from it).

The tokenizer, not a regex, decides where a literal begins and ends: a routine body is
dollar-quoted, and an apostrophe in a comment inside it (``don't``) would shift any
quote-pairing regex and leave a connection string unmasked. Each literal's whole body is judged
by the predicate and replaced whole, so a secret is never half-replaced.

The decision is made per literal and per comment, never per routine body: masking a whole body
would erase exactly the diff this tool exists to show.
"""

from __future__ import annotations

import hashlib
import re

from cumo_schema_comparer.literals import (
    SECRET_KEYWORDS,
    looks_like_connection_string,
    looks_like_credential_url,
)
from cumo_schema_comparer.normalize.tokenizer import (
    DOLLAR_TAG,
    Token,
    TokenType,
    scan_single_quoted,
    tokenize_with_comments,
)

MASK_PREFIX = "***:"

#: Dollar-quote nesting we are willing to recurse through; a body nested deeper is judged as one
#: blob rather than risk the recursion limit. No real routine nests tags this deep.
_MAX_DEPTH = 32

#: Exactly what ``_mask_for`` produces. A forced literal that is already one is not masked again,
#: which is what keeps masking idempotent; nothing else is exempt.
_MASK = re.compile(re.escape(MASK_PREFIX) + r"[0-9a-f]{12}")

_SECRET_WORDS = frozenset(SECRET_KEYWORDS)


def _mask_for(secret: str) -> str:
    """A stable, non-reversible stand-in that still differs between different secrets.

    No per-run or per-host salt: master and target must produce the same mask for the same
    literal, or an unchanged credential would report as drift on every run. 12 hex digits is
    48 bits.
    """
    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]
    return f"{MASK_PREFIX}{digest}"


def _mask_quoted(text: str, *, force: bool) -> str:
    """Mask one single-quoted literal token (``'..'``, ``E'..'``, ``U&'..'``).

    ``force`` masks it whatever its shape: the literal follows a secret keyword and ``=``.
    """
    quote = text.index("'")
    prefix = text[:quote]
    backslashes = prefix[:1] in ("e", "E")
    # The tokenizer treats an unterminated literal as running to the end of input. Appending a
    # non-quote character tells the two cases apart: a terminated literal still ends where it did,
    # an unterminated one swallows the extra character.
    terminated = scan_single_quoted(text + "x", quote, backslash_escapes=backslashes) == len(text)
    body = text[quote + 1 : -1] if terminated else text[quote + 1 :]
    if _MASK.fullmatch(body) or (not force and not looks_like_connection_string(body)):
        return text
    return f"{prefix}'{_mask_for(body)}" + ("'" if terminated else "")


def _split_dollar(text: str) -> tuple[str, str, str]:
    """``(opening tag, inner, closing tag)``; the closing tag is empty when unterminated."""
    tag_match = DOLLAR_TAG.match(text)
    tag = tag_match.group(0) if tag_match else ""
    terminated = len(text) >= 2 * len(tag) and text.endswith(tag)
    if terminated:
        return tag, text[len(tag) : -len(tag)], tag
    return tag, text[len(tag) :], ""


def _code_residue(inner: str, tokens: list[Token]) -> str:
    """``inner`` with its string literals and comments cut out."""
    pieces: list[str] = []
    cursor = 0
    for token in tokens:
        if token.type in (TokenType.STRING, TokenType.COMMENT):
            pieces.append(inner[cursor : token.start])
            cursor = token.start + len(token.text)
    pieces.append(inner[cursor:])
    return " ".join(pieces)


def _mask_dollar(text: str, depth: int, *, force: bool) -> str:
    tag, inner, closing = _split_dollar(text)
    if force:
        return text if _MASK.fullmatch(inner) else f"{tag}{_mask_for(inner)}{closing}"
    if depth >= _MAX_DEPTH:
        masked = _mask_for(inner) if looks_like_connection_string(inner) else inner
        return f"{tag}{masked}{closing}"
    tokens = tokenize_with_comments(inner)
    # A connection URL left over once literals and comments are cut out cannot be valid code, so
    # this dollar quote is a plain string value rather than a routine body. Only the URL shapes
    # count here: ``password = other_col`` is perfectly good code.
    if looks_like_credential_url(_code_residue(inner, tokens)):
        return f"{tag}{_mask_for(inner)}{closing}"
    return f"{tag}{_mask_tokens(inner, tokens, depth + 1)}{closing}"


def _mask_comment(text: str) -> str:
    """Mask the content of a comment whole, keeping its delimiters."""
    if text.startswith("--"):
        body = text[2:].rstrip("\r\n")
        ending = text[2 + len(body) :]
        if not looks_like_connection_string(body):
            return text
        return f"-- {_mask_for(body)}{ending}"
    terminated = len(text) >= 4 and text.endswith("*/")
    body = text[2:-2] if terminated else text[2:]
    if not looks_like_connection_string(body):
        return text
    return f"/* {_mask_for(body)} */" if terminated else f"/* {_mask_for(body)}"


#: Operator-token sequences that bind a value to a name. The tokenizer emits ``:=`` and ``=>`` as
#: two tokens each.
_ASSIGNMENTS = (("=",), (":", "="), ("=", ">"))


def _follows_secret_keyword(recent: list[Token]) -> bool:
    """Whether the tokens just before a literal are ``<keyword> =``, ``:=`` or ``=>``.

    ``:=`` is the plpgsql assignment and ``=>`` the named-argument arrow. Comparisons such as
    ``<=``, ``>=`` and ``<>`` are single other tokens and deliberately do not count: they are not
    assignments. ``=`` stays because it is the libpq form and also assigns in ``SET``.
    """
    for operators in _ASSIGNMENTS:
        count = len(operators)
        if len(recent) <= count:
            continue
        tail = recent[-count:]
        keyword = recent[-count - 1]
        if (
            all(t.type is TokenType.OPERATOR for t in tail)
            and tuple(t.text for t in tail) == operators
            and keyword.type is TokenType.WORD
            and keyword.text.lower() in _SECRET_WORDS
        ):
            return True
    return False


def _mask_tokens(text: str, tokens: list[Token], depth: int) -> str:
    pieces: list[str] = []
    cursor = 0
    recent: list[Token] = []  # the last three non-comment tokens, oldest first
    for token in tokens:
        if token.type is TokenType.COMMENT:
            replacement = _mask_comment(token.text)
        elif token.type is TokenType.STRING:
            force = _follows_secret_keyword(recent)
            if token.text.startswith("$"):
                replacement = _mask_dollar(token.text, depth, force=force)
            else:
                replacement = _mask_quoted(token.text, force=force)
        else:
            replacement = None
        if token.type is not TokenType.COMMENT:
            recent = [*recent[-2:], token]
        if replacement is not None:
            pieces.append(text[cursor : token.start])
            pieces.append(replacement)
            cursor = token.start + len(token.text)
    pieces.append(text[cursor:])
    return "".join(pieces)


def mask_literals(text: str | None) -> str | None:
    """Mask credentials in ``text`` one literal or comment at a time, keeping the SQL around them.

    Masked: a string literal whose own body is credential-shaped; the literal right after a secret
    keyword and ``=`` (``password = 'x'``), whatever its shape; and a comment whose content is
    credential-shaped. Inside a dollar-quoted body the inner literals and comments are handled the
    same way, and the body itself is never masked.

    A masked piece no longer looks like a credential, so applying this twice equals applying it
    once; there is deliberately no "starts with the mask prefix" skip, which would let
    ``'***:postgresql://u:pw@h'`` through.

    An unterminated literal runs to the end of the input (the tokenizer's rule), so it is judged
    and masked as a whole, and left unterminated.
    """
    if text is None:
        return None
    return _mask_tokens(text, tokenize_with_comments(text), 0)
