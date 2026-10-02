"""Which key a column's raw text is filed under.

``raw`` is read by two things that both key off the attribute name: the cosmetic-delta check, and
the display getter that a rendered diff is drawn from. So filing a value under the wrong key does
not lose it — it prints it under the wrong heading, next to a compared value that is empty.

A stored generated column is the case where that can happen, because its expression lives in the
same ``pg_attrdef`` slot as a default and is read by the same ``pg_get_expr(adbin)`` call.
"""

from cumo_schema_comparer.build import _columns
from cumo_schema_comparer.model.objects import SERIAL_SENTINEL


def column(**row):
    base = {
        "schema": "public",
        "name": "t",
        "subname": "c",
        "ordinal": 1,
        "data_type": "integer",
        "is_nullable": True,
    }
    (built,) = _columns([{**base, **row}]).values()
    return built


EXPRESSION = "(amount_scaled * 2::numeric)"


class TestAStoredGeneratedColumn:
    def built(self):
        return column(
            data_type="numeric",
            generated_kind="s",
            default_expr=EXPRESSION,
            generated_expr=EXPRESSION,
        )

    def test_the_expression_is_filed_under_generated(self):
        assert self.built().raw.get("generated") == EXPRESSION

    def test_nothing_is_filed_under_default(self):
        # The defect: the display getter for `column.default` would find this and render a
        # generation expression beside an empty compared value.
        built = self.built()
        assert built.default is None
        assert built.raw.get("default") is None
        assert "default" not in built.raw.values

    def test_the_compared_value_is_still_the_canonical_expression(self):
        assert self.built().generated is not None


class TestAnOrdinaryDefault:
    def test_the_text_is_filed_under_default_and_not_under_generated(self):
        built = column(default_expr="42")
        assert built.raw.get("default") == "42"
        assert built.raw.get("generated") is None
        assert built.default == "42"

    def test_a_column_with_no_default_files_neither(self):
        built = column()
        assert set(built.raw.values) == {"data_type"}


class TestASerialColumn:
    """The benign variant, which must keep working: the compared value is a sentinel, so the raw
    ``nextval(...)`` is the only text a reader can be shown."""

    def built(self):
        return column(default_expr="nextval('public.t_c_seq'::regclass)", owned_sequence="t_c_seq")

    def test_the_nextval_is_still_filed_under_default(self):
        assert "nextval(" in (self.built().raw.get("default") or "")

    def test_the_compared_value_is_the_serial_sentinel(self):
        assert self.built().default == SERIAL_SENTINEL

    def test_nothing_is_filed_under_generated(self):
        assert self.built().raw.get("generated") is None


class TestAnIdentityColumn:
    def test_an_identity_column_is_not_treated_as_generated(self):
        # `attidentity`, not `attgenerated`: there is no expression to file at all.
        built = column(identity="d")
        assert built.generated is None
        assert set(built.raw.values) == {"data_type"}
