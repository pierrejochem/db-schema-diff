"""Column default canonicalization, including the serial sentinel."""

import pytest

from db_schema_comparer.model.objects import SERIAL_SENTINEL
from db_schema_comparer.normalize.defaults import canonical_default, owned_sequence_name


class TestSerial:
    """A `serial` column's default names a generated sequence whose name varies per database.

    ``t_id_seq`` here, ``t_id_seq1`` there after some historical rename, so comparing the
    literal default reports drift on every serial column in the schema.
    """

    def test_a_nextval_on_an_owned_sequence_becomes_the_sentinel(self):
        result = canonical_default(
            "nextval('invoice_id_seq'::regclass)",
            column_type="int4",
            owned_sequence="invoice_id_seq",
        )
        assert result == SERIAL_SENTINEL

    def test_a_qualified_owned_sequence_also_becomes_the_sentinel(self):
        result = canonical_default(
            "nextval('\"acme-invoicing\".invoice_id_seq'::regclass)",
            column_type="int4",
            owned_sequence="invoice_id_seq",
        )
        assert result == SERIAL_SENTINEL

    def test_a_renamed_sequence_still_matches_because_the_name_is_not_compared(self):
        left = canonical_default(
            "nextval('t_id_seq'::regclass)", column_type="int4", owned_sequence="t_id_seq"
        )
        right = canonical_default(
            "nextval('t_id_seq1'::regclass)", column_type="int4", owned_sequence="t_id_seq1"
        )
        assert left == right == SERIAL_SENTINEL

    def test_a_nextval_on_a_sequence_the_column_does_not_own_is_kept_verbatim(self):
        # A shared sequence is a deliberate design choice, and which one it is matters.
        result = canonical_default(
            "nextval('shared_id_seq'::regclass)", column_type="int4", owned_sequence=None
        )
        assert result != SERIAL_SENTINEL
        assert "shared_id_seq" in result

    def test_a_nextval_on_a_different_owned_sequence_is_kept(self):
        result = canonical_default(
            "nextval('other_seq'::regclass)", column_type="int4", owned_sequence="invoice_id_seq"
        )
        assert result != SERIAL_SENTINEL


class TestOwnedSequenceName:
    @pytest.mark.parametrize(
        ("default", "expected"),
        [
            ("nextval('invoice_id_seq'::regclass)", "invoice_id_seq"),
            ("nextval('public.invoice_id_seq'::regclass)", "invoice_id_seq"),
            ('nextval(\'"acme-invoicing"."Inv_id_seq"\'::regclass)', "Inv_id_seq"),
            ("NEXTVAL('invoice_id_seq'::regclass)", "invoice_id_seq"),
            ("nextval('invoice_id_seq')", "invoice_id_seq"),
        ],
    )
    def test_the_sequence_name_is_extracted(self, default, expected):
        assert owned_sequence_name(default) == expected

    @pytest.mark.parametrize("default", ["now()", "0", None, "", "gen_random_uuid()"])
    def test_a_non_nextval_default_has_no_sequence(self, default):
        assert owned_sequence_name(default) is None


class TestPresentationOnly:
    @pytest.mark.parametrize(
        ("raw", "column_type", "expected"),
        [
            ("'foo'::character varying", "varchar(50)", "'foo'"),
            ("(0)::numeric", "numeric(10,2)", "0"),
            ("(-1)", "int4", "-1"),
            ("((0))", "int4", "0"),
            ("NULL::text", "text", "null"),  # keywords fold to lower case
            ("'{}'::text[]", "text[]", "'{}'"),
        ],
    )
    def test_redundant_presentation_is_removed(self, raw, column_type, expected):
        assert canonical_default(raw, column_type=column_type) == expected

    def test_a_meaningful_cast_survives(self):
        assert "::text" in canonical_default("'foo'::text", column_type="varchar(50)")

    @pytest.mark.parametrize(
        ("left", "right"),
        [("now()", "CURRENT_TIMESTAMP"), ("now()", "('now'::text)::timestamp with time zone")],
    )
    def test_equivalent_now_spellings_converge(self, left, right):
        assert canonical_default(left, column_type="timestamptz") == canonical_default(
            right, column_type="timestamptz"
        )

    def test_no_default_stays_none(self):
        assert canonical_default(None, column_type="int4") is None

    def test_a_function_call_default_is_preserved(self):
        assert canonical_default("gen_random_uuid()", column_type="uuid") == "gen_random_uuid()"

    def test_a_schema_qualified_function_default_keeps_its_qualifier(self):
        # With search_path='' the server always qualifies, so the qualifier is meaningful and
        # comparable rather than noise.
        result = canonical_default("public.gen_uuid()", column_type="uuid")
        assert result == "public.gen_uuid()"
