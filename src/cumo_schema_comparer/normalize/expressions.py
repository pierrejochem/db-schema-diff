"""Expression canonicalization.

``pg_get_expr`` and its relatives re-print expressions from the parse tree, so the same
original DDL comes back with parentheses and casts the author never wrote, and in a form that
changes between PostgreSQL major releases. Comparing that text directly reports drift on
schemas that are identical.

The approach is deliberately narrow: normalise **presentation** (whitespace, redundant
parentheses, redundant casts, keyword case, type spelling) and stop. It is not a semantic
engine, and it will not try to prove that two different expressions compute the same value.
Anything beyond presentation is reported for a human to judge.
"""

from __future__ import annotations

from .identifiers import quote, unquote
from .tokenizer import Token, TokenType, tokenize
from .types import canonical_type, cast_is_redundant

#: Spellings of the same value that PostgreSQL prints inconsistently across versions and
#: contexts. Deliberately short: each entry is a claim that two texts are *always*
#: interchangeable, and a wrong entry silently hides real drift.
_EQUIVALENCES: dict[str, str] = {
    "now()": "now()",
    "current_timestamp": "now()",
    "('now'::text)::timestamp with time zone": "now()",
    "('now'::text)::timestamptz": "now()",
    "transaction_timestamp()": "now()",
    "current_user": "current_user",
    '"current_user"()': "current_user",
    "current_role": "current_user",
    "session_user": "session_user",
    '"session_user"()': "session_user",
    "current_schema": "current_schema()",
    "current_schema()": "current_schema()",
    "current_database()": "current_database()",
}

#: Tokens after which a ``-`` or ``+`` is a sign rather than a binary operator.
_PREFIX_CONTEXT = frozenset({"(", ",", "="})

#: Operators printed without surrounding spaces.
_TIGHT = frozenset({"::"})


def canonical_expr(raw: str | None, *, column_type: str | None = None) -> str | None:
    """Canonical form of one expression.

    ``column_type`` enables redundant-cast removal: a cast to the column's own type carries no
    information, whereas a cast to any other type does. Without it, no cast is assumed
    redundant.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return text

    tokens = _drop_trailing_semicolon(tokenize(text))
    if not tokens:
        return ""

    # Equivalences are matched before casts are stripped as well as after, because some of
    # them *are* a cast: `('now'::text)::timestamp with time zone` is how the server spells
    # `now()`, and stripping its outer cast first would destroy the pattern.
    intact = _strip_outer_parens(_render(tokens))
    known = _EQUIVALENCES.get(intact.lower())
    if known is not None:
        return known

    stripped = _strip_outer_parens(_render(_strip_redundant_casts(tokens, column_type)))
    return _EQUIVALENCES.get(stripped.lower(), stripped)


def exprs_equivalent(
    left: str | None, right: str | None, *, column_type: str | None = None
) -> bool:
    """Whether two expression spellings are the same expression."""
    return canonical_expr(left, column_type=column_type) == canonical_expr(
        right, column_type=column_type
    )


def _drop_trailing_semicolon(tokens: list[Token]) -> list[Token]:
    if tokens and tokens[-1].type is TokenType.PUNCTUATION and tokens[-1].text == ";":
        return tokens[:-1]
    return tokens


def _strip_redundant_casts(tokens: list[Token], column_type: str | None) -> list[Token]:
    """Drop a trailing ``::T`` when ``T`` is the column's own type *and* it casts everything.

    ``'foo'::character varying`` on a ``varchar`` column coerces the whole default and says
    nothing, so it goes. ``'foo'::text`` on the same column says something real, so it stays.

    The "everything" part is not a nicety. A real ``GENERATED`` column comes back from the server
    as ``(amount * 2::numeric)``, where the cast binds to one operand. Dropping it would turn
    numeric division into integer division in the canonical form, and the tool would then report
    two genuinely different expressions as identical — the one failure a drift detector must not
    have.
    """
    if column_type is None:
        return tokens
    if not _is_whole_expression_cast(tokens):
        return tokens

    index = _trailing_cast_index(tokens)
    if index is None:
        return tokens
    _, cast_type = _read_cast_type(tokens, index + 1)
    if not cast_is_redundant(cast_type, column_type):
        return tokens
    return tokens[:index]


def _trailing_cast_index(tokens: list[Token]) -> int | None:
    """Index of a ``::`` that is the last operator in the expression, at paren depth zero."""
    depth = 0
    found: int | None = None
    for index, token in enumerate(tokens):
        if token.text == "(":
            depth += 1
        elif token.text == ")":
            depth -= 1
        elif depth == 0 and token.type is TokenType.OPERATOR and token.text == "::":
            found = index
    if found is None:
        return None
    # Everything after it must be the type name, not further expression.
    consumed, cast_type = _read_cast_type(tokens, found + 1)
    return found if cast_type is not None and consumed == len(tokens) else None


def _is_whole_expression_cast(tokens: list[Token]) -> bool:
    """Whether a trailing cast applies to the whole expression rather than to one operand.

    True when what precedes the ``::`` is a single primary: one literal or identifier, or a
    parenthesised group that spans from the start.
    """
    index = _trailing_cast_index(tokens)
    if index is None:
        return False
    operand = tokens[:index]
    if len(operand) == 1:
        return True
    if not operand or operand[0].text != "(":
        return False
    # A parenthesised group only wraps the whole operand if its closing paren is the last token.
    depth = 0
    for position, token in enumerate(operand):
        if token.text == "(":
            depth += 1
        elif token.text == ")":
            depth -= 1
            if depth == 0:
                return position == len(operand) - 1
    return False


def _read_cast_type(tokens: list[Token], start: int) -> tuple[int, str | None]:
    """Read the type name following a ``::``.

    Returns the index just past it and the type text, or ``(start, None)`` when what follows
    is not a plain type name (a qualified user type, say, which is never treated as redundant).
    """
    words: list[str] = []
    index = start
    while index < len(tokens) and tokens[index].type is TokenType.WORD:
        words.append(tokens[index].text)
        index += 1
    if not words:
        return start, None
    # A qualified name is a user type; leave those casts alone.
    if index < len(tokens) and tokens[index].text == ".":
        return start, None
    modifier = ""
    if index < len(tokens) and tokens[index].text == "(":
        depth = 0
        close = index
        while close < len(tokens):
            if tokens[close].text == "(":
                depth += 1
            elif tokens[close].text == ")":
                depth -= 1
                if depth == 0:
                    break
            close += 1
        if close >= len(tokens):
            return start, None
        modifier = "(" + "".join(t.text for t in tokens[index + 1 : close]) + ")"
        index = close + 1
    array = ""
    while index + 1 < len(tokens) and tokens[index].text == "[" and tokens[index + 1].text == "]":
        array = "[]"
        index += 2
    return index, " ".join(words) + modifier + array


def _render(tokens: list[Token]) -> str:
    """Print tokens back with canonical spacing, case and type names."""
    pieces: list[tuple[str, Token]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]

        if token.type is TokenType.OPERATOR and token.text == "::":
            consumed, cast_type = _read_cast_type(tokens, index + 1)
            if cast_type is not None:
                pieces.append(("::", token))
                pieces.append((canonical_type(cast_type) or cast_type, tokens[index + 1]))
                index = consumed
                continue

        pieces.append((_render_token(token), token))
        index += 1

    return _join(pieces)


def _render_token(token: Token) -> str:
    if token.type is TokenType.WORD:
        # Unquoted identifiers and keywords are case-insensitive to PostgreSQL, so folding
        # them removes a difference that is not one.
        return token.text.lower()
    return token.text


def _join(pieces: list[tuple[str, Token]]) -> str:
    """Join rendered pieces, inserting a space only where one is needed."""
    out: list[str] = []
    for position, piece in enumerate(pieces):
        if position and _needs_space(pieces, position):
            out.append(" ")
        out.append(piece[0])
    return "".join(out)


def _needs_space(pieces: list[tuple[str, Token]], position: int) -> bool:
    """Whether a space belongs before ``pieces[position]``.

    The aim is a single consistent spelling, not pretty SQL: both sides of a comparison are
    rendered by this same function, so what matters is that equivalent inputs converge. A word
    followed by ``(`` is therefore always printed tight, which makes ``ANY (ARRAY[...])`` and
    ``any(array[...])`` — the same expression printed by two server versions — converge.
    """
    previous = pieces[position - 1][0]
    current = pieces[position][0]

    if previous in _TIGHT or current in _TIGHT:
        return False
    if current in {")", ",", ".", "]", ";", "(", "["}:
        return False
    if previous in {"(", ".", "["}:
        return False
    # A sign binds to its operand: `(-1)` must not become `(- 1)`, or the same default written
    # two ways would compare unequal.
    return not _is_sign(pieces, position - 1)


def _is_sign(pieces: list[tuple[str, Token]], position: int) -> bool:
    """Whether ``pieces[position]`` is a sign rather than a binary operator.

    A sign opens the expression, or follows an opening bracket, a comma, or another operator.
    """
    text, token = pieces[position]
    if text not in {"-", "+"} or token.type is not TokenType.OPERATOR:
        return False
    if position == 0:
        return True
    before_text, before_token = pieces[position - 1]
    if before_text in {"(", ",", "["}:
        return True
    return before_token.type is TokenType.OPERATOR


def _strip_outer_parens(text: str) -> str:
    """Remove parentheses that wrap the whole expression.

    ``pg_get_expr`` adds them liberally: a default of ``0`` comes back as ``(0)``, and a check
    constraint gets a layer per nesting level. Only balanced, genuinely outermost pairs are
    removed, so ``(a + b) * c`` keeps its parentheses.
    """
    result = text.strip()
    while result.startswith("(") and result.endswith(")"):
        depth = 0
        wraps_whole = True
        for position, char in enumerate(result):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and position != len(result) - 1:
                    wraps_whole = False
                    break
        if depth != 0 or not wraps_whole:
            return result
        result = result[1:-1].strip()
    return result


def remap_schema_qualifiers(raw: str | None, mapping: dict[str, str]) -> str | None:
    """Rewrite schema qualifiers in an expression from one namespace into another.

    Only names in a schema position are touched, so a column that happens to share a schema's
    name is left alone. String literals are not rewritten either, with one deliberate
    exception: ``nextval('qa.t_id_seq'::regclass)`` names a schema *inside* a literal, and
    leaving it alone would report drift on every serial column.
    """
    if raw is None or not mapping:
        return raw

    tokens = tokenize(raw)
    pieces: list[str] = []
    cursor = 0

    for index, token in enumerate(tokens):
        replacement: str | None = None

        if (
            token.is_quotable
            and _followed_by_dot(tokens, index)
            and not _preceded_by_dot(tokens, index)
        ):
            name = unquote(token.text)
            if name in mapping:
                replacement = quote(mapping[name])
        elif token.type is TokenType.STRING and _is_regclass_literal(tokens, index):
            replacement = _remap_regclass_literal(token.text, mapping)

        if replacement is not None:
            pieces.append(raw[cursor : token.start])
            pieces.append(replacement)
            cursor = token.start + len(token.text)

    pieces.append(raw[cursor:])
    return "".join(pieces)


def _followed_by_dot(tokens: list[Token], index: int) -> bool:
    return index + 1 < len(tokens) and tokens[index + 1].text == "."


def _preceded_by_dot(tokens: list[Token], index: int) -> bool:
    return index > 0 and tokens[index - 1].text == "."


def _is_regclass_literal(tokens: list[Token], index: int) -> bool:
    """Whether this literal is cast to ``regclass``, i.e. it names a relation."""
    return (
        index + 2 < len(tokens)
        and tokens[index + 1].text == "::"
        and tokens[index + 2].text.lower() in {"regclass", "regproc", "regtype", "regnamespace"}
    )


def _remap_regclass_literal(literal: str, mapping: dict[str, str]) -> str | None:
    """Rewrite the schema part of a ``'schema.relation'`` literal."""
    body = literal[1:-1] if literal.startswith("'") and literal.endswith("'") else literal
    inner_tokens = tokenize(body)
    if len(inner_tokens) < 3 or inner_tokens[1].text != ".":
        return None
    schema = unquote(inner_tokens[0].text)
    if schema not in mapping:
        return None
    rest = body[inner_tokens[2].start :]
    return f"'{quote(mapping[schema])}.{rest}'"
