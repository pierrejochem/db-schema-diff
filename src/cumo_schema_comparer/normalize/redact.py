"""Masking credentials in definition text (literals and comments), keeping the SQL around them.

Lives in ``normalize`` because it needs the tokenizer, and ``literals`` must stay stdlib-only
(the GUI re-exports from it).

The tokenizer, not a regex, decides where a literal begins and ends: a routine body is
dollar-quoted, and an apostrophe in a comment inside it (``don't``) would shift any
quote-pairing regex and leave a connection string unmasked. Each literal's whole body is judged
by the predicate and replaced whole, so a secret is never half-replaced.

The decision is made per literal, per comment and per credential-bearing span, never per routine
body: masking a whole body would erase exactly the diff this tool exists to show.

Two structural rules carry the weight, because a keyword alone cannot tell a value from code:

* A dollar quote is masked whole only when its residue is a credential-bearing *value* — a
  connection URL, or nothing but ``key=value`` pairs. A routine body has bare tokens and is
  recursed into instead.
* A comment is free text, so only the offending spans inside it are masked and the rest of the
  text survives for whoever reads the diff.
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

#: ``\s*=\s*`` collapsed to ``=``, so ``host = db1`` and ``host=db1`` are judged alike: libpq
#: accepts both spellings of a conninfo pair.
_AROUND_EQUALS = re.compile(r"\s*=\s*")

#: One ``key=value`` pair — the only thing a libpq conninfo is made of.
_CONNINFO_PAIR = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=\S*")

#: A URL whose userinfo carries a password (``scheme://user:secret@host``). Narrower on purpose
#: than ``literals.looks_like_credential_url``: in free text a bare ``git@github.com`` or
#: ``user@host`` names an account, not a secret, and masking it would delete the git and
#: documentation URLs a reviewer of a drift diff is reading the comment for. The scheme is
#: anchored and neither character class can straddle the separator it looks for, so the scan is
#: linear with nothing to backtrack over.
_PASSWORD_IN_URL = re.compile(
    r"(?<![a-z0-9+.-])[0-9+.-]*[a-z][a-z0-9+.-]*://[^/?#\s@:]*:[^/?#\s@]*@", re.IGNORECASE
)

#: A secret keyword bound tight to its value, the libpq conninfo form, as it reads in free text.
#: The value may be quoted and then carries spaces (``password='P@ss word'``), which is why this
#: pass must run before the token scan: the token scan alone would mask ``password='P@ss`` and
#: leave ``word'`` behind, which is the leak the ordering exists to prevent (``gui/app.py``'s
#: ``_sanitise`` pins the same rule). Only the tight ``=`` counts — ``password = NULL`` is prose
#: about a column, and masking it destroys the comment rather than a secret.
_COMMENT_SECRET = re.compile(
    r"(?<![\w-])(?:"
    + "|".join(SECRET_KEYWORDS)
    + r")="
    + r"(?:'(?:\\.|[^'\\\n])*'?|\"(?:\\.|[^\"\\\n])*\"?|\S*)",
    re.IGNORECASE,
)

#: A whitespace-delimited token. Substituting over these keeps every byte of surrounding
#: whitespace, so a masked multi-line comment still has its line breaks.
_NON_SPACE = re.compile(r"\S+")


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
    # An empty literal carries no secret, and masking it would publish the sha256 of the empty
    # string — a well-known constant that announces the password as empty.
    if not body or _MASK.fullmatch(body) or (not force and not looks_like_connection_string(body)):
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


def _is_conninfo(residue: str) -> bool:
    """Whether ``residue`` is nothing but ``key=value`` pairs.

    This is the structural difference between a conninfo *value* and a routine *body*, and it is
    structural rather than a question of which keyword appears. Every token of ``host=db1 user=u
    password=s3cret dbname=d`` is a pair; ``BEGIN UPDATE t SET password = other_col; END`` has
    bare tokens (``BEGIN``, ``UPDATE``, ``t``, ``SET``, ``END``) that no conninfo ever has.
    Keying off the keyword instead either leaks the first or destroys the second — both have
    happened here.
    """
    tokens = _AROUND_EQUALS.sub("=", residue).split()
    return bool(tokens) and all(_CONNINFO_PAIR.fullmatch(token) for token in tokens)


def _is_credential_value(residue: str) -> bool:
    """Whether a dollar quote's code residue is a credential-bearing *value*, not code.

    Two shapes qualify and nothing else. A connection URL cannot be valid code, so a dollar
    quote whose residue holds one is a plain string value (``$q$postgresql://u:p@h/d$q$`` is
    legal SQL). And a libpq conninfo is only ``key=value`` pairs, so a residue made of nothing
    else that also names a secret keyword is one — ``dblink_connect($$host=db1
    password=s3cret dbname=d$$)`` and ``postgres_fdw`` options are precisely what this module
    exists to catch. Code is left alone either way, because ``password = other_col`` is good SQL
    and a body masked whole destroys the diff this tool is for.
    """
    if looks_like_credential_url(residue):
        return True
    # The cheap keyword test first: it is false for nearly every body, and it keeps the pair scan
    # off the 200 KB residues that carry no secret keyword at all.
    return looks_like_connection_string(residue) and _is_conninfo(residue)


def _mask_dollar(text: str, depth: int, *, force: bool) -> str:
    tag, inner, closing = _split_dollar(text)
    if force:
        if not inner or _MASK.fullmatch(inner):
            return text
        return f"{tag}{_mask_for(inner)}{closing}"
    if depth >= _MAX_DEPTH:
        # Past the cap nothing has looked inside, so judge the blob with the wide predicate and
        # fail closed. No real routine nests tags this deep.
        masked = _mask_for(inner) if looks_like_connection_string(inner) else inner
        return f"{tag}{masked}{closing}"
    tokens = tokenize_with_comments(inner)
    if _is_credential_value(_code_residue(inner, tokens)):
        return f"{tag}{_mask_for(inner)}{closing}"
    return f"{tag}{_mask_tokens(inner, tokens, depth + 1)}{closing}"


def _mask_free_text(text: str) -> str:
    """Mask the credential-bearing spans of free text, leaving every other byte in place.

    The keyword pass runs first and the token scan second; see ``_COMMENT_SECRET`` for why that
    order is the one that does not leak.
    """

    def mask_token(match: re.Match[str]) -> str:
        token = match.group(0)
        return _mask_for(token) if _PASSWORD_IN_URL.search(token) else token

    bound = _COMMENT_SECRET.sub(lambda match: _mask_for(match.group(0)), text)
    return _NON_SPACE.sub(mask_token, bound)


def _mask_comment(text: str) -> str:
    """Mask the credential-bearing spans of a comment, keeping its delimiters and its prose.

    A comment is free text, not SQL, so masking *within* it cannot change what the definition
    means — and the comments in a view or routine body are exactly the context a reviewer of a
    drift diff needs. Replacing the content whole, which is what this did before, destroyed every
    comment that merely mentioned ``password =`` or cited a git or documentation URL.
    """
    if text.startswith("--"):
        return "--" + _mask_free_text(text[2:])
    if len(text) >= 4 and text.endswith("*/"):
        return "/*" + _mask_free_text(text[2:-2]) + "*/"
    return "/*" + _mask_free_text(text[2:])


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
    """Mask credentials in ``text`` one literal, comment or span at a time, keeping the SQL.

    Masked: a string literal whose own body is credential-shaped; the literal right after a secret
    keyword and ``=``, ``:=`` or ``=>`` (``password = 'x'``), whatever its shape; the
    credential-bearing spans of a comment; and a dollar quote whose residue is a credential value
    rather than code (see :func:`_is_credential_value`). An empty literal is left alone: it holds
    no secret. Inside a dollar-quoted body the inner literals and comments are handled the same
    way, and a body that is code is never masked whole.

    A masked piece no longer looks like a credential, so applying this twice equals applying it
    once; there is deliberately no "starts with the mask prefix" skip, which would let
    ``'***:postgresql://u:pw@h'`` through.

    An unterminated literal runs to the end of the input (the tokenizer's rule), so it is judged
    and masked as a whole, and left unterminated.

    **What the mask does and does not guarantee.** It is an unsalted sha256 truncated to 48 bits,
    so it is a *stable pseudonym, not confidentiality*: anyone holding a report can confirm a
    guess with a single hash, and ``***:f52fbd32b2b3`` is ``hunter2`` to whoever tries it. That is
    the accepted price of the property the tool needs — master and target must mask an unchanged
    value identically, or every run would report drift, and a changed credential must mask
    differently, or a rotation would report as no change. A salt would buy secrecy and lose both.
    Treat a masked report as "the secret is not quoted in full", never as "the secret is
    protected"; a weak or already-known value stays guessable.
    """
    if text is None:
        return None
    return _mask_tokens(text, tokenize_with_comments(text), 0)
