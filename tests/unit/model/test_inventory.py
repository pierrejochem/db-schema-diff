"""Inventory semantics and the JSON round trip that offline comparison depends on."""

import pytest

from cumo_schema_comparer.model.inventory import Inventory
from cumo_schema_comparer.model.keys import ObjectKey, column_key, table_key
from cumo_schema_comparer.model.kinds import KIND_ORDER, ObjectKind
from cumo_schema_comparer.model.objects import SERIAL_SENTINEL, Column, RawValues
from tests.support.builders import changelog, changeset, col, inventory, table


def sample():
    return inventory(
        table(
            "cumo-invoicing",
            "invoice",
            cols=[col("id", "int4", nullable=False), col("amount", "numeric(10,2)")],
        ),
        table("public", "mandant", cols=[col("name", "varchar(80)")]),
        changelog_state=changelog(changeset("update_1", order=1, tag="R7.6.2")),
    )


class TestRoundTrip:
    def test_json_round_trip_preserves_every_object(self):
        original = sample()
        restored = Inventory.from_json(original.to_json())
        assert restored.objects == original.objects
        assert restored.schemas == original.schemas
        assert restored.source == original.source

    def test_json_round_trip_preserves_the_changelog(self):
        restored = Inventory.from_json(sample().to_json())
        assert restored.changelog is not None
        assert restored.changelog.count == 1
        assert restored.changelog.last_tag == "R7.6.2"
        assert restored.changelog.location.schema == "cumo-invoicing"

    def test_fingerprint_survives_the_round_trip(self):
        original = sample()
        assert Inventory.from_json(original.to_json()).fingerprint() == original.fingerprint()

    def test_raw_values_survive_but_do_not_affect_equality(self):
        key = column_key("public", "t", "c")
        with_raw = Column(
            key=key,
            ordinal=1,
            data_type="varchar(10)",
            is_nullable=True,
            raw=RawValues({"data_type": "character varying(10)"}),
        )
        without = Column(key=key, ordinal=1, data_type="varchar(10)", is_nullable=True)
        assert with_raw == without  # raw text is display-only
        restored = Inventory.from_json(inventory({key: with_raw}).to_json())
        assert restored.objects[key].raw.get("data_type") == "character varying(10)"

    def test_serial_sentinel_survives_the_round_trip(self):
        key = column_key("public", "t", "id")
        column = Column(
            key=key,
            ordinal=1,
            data_type="int4",
            is_nullable=False,
            default=SERIAL_SENTINEL,
            sequence_name="t_id_seq",
        )
        restored = Inventory.from_json(inventory({key: column}).to_json())
        assert restored.objects[key].is_serial
        assert restored.objects[key].autoincrement == "serial"

    def test_an_unknown_schema_version_is_refused(self):
        payload = sample().to_json_dict()
        payload["schema_version"] = 99
        with pytest.raises(ValueError, match="schema_version"):
            Inventory.from_json_dict(payload)

    def test_an_unreadable_attribute_is_refused_rather_than_ignored(self):
        # A newer build's extra attribute must not be silently dropped: comparing a subset of
        # the data and reporting "in sync" is the worst possible failure for this tool.
        payload = sample().to_json_dict()
        payload["objects"][0]["attributes"]["invented_later"] = True
        with pytest.raises(ValueError, match="invented_later"):
            Inventory.from_json_dict(payload)


class TestQueries:
    def test_by_kind_selects_one_kind(self):
        inv = sample()
        assert set(inv.by_kind(ObjectKind.TABLE)) == {
            table_key("cumo-invoicing", "invoice"),
            table_key("public", "mandant"),
        }
        assert len(inv.by_kind(ObjectKind.COLUMN)) == 3

    def test_counts_follow_report_order(self):
        counts = sample().counts()
        assert list(counts) == [k for k in KIND_ORDER if k in counts]
        assert counts == {ObjectKind.TABLE: 2, ObjectKind.COLUMN: 3}

    def test_sorted_keys_are_deterministic_and_case_sensitive(self):
        inv = inventory(table("public", "QRTZ_LOCKS"), table("public", "invoice"))
        names = [k.name for k in inv.sorted_keys()]
        # Uppercase sorts first; identifiers are never case-folded.
        assert names == ["QRTZ_LOCKS", "invoice"]

    def test_is_empty_detects_a_brand_new_database(self):
        assert inventory().is_empty
        assert not sample().is_empty


class TestRemapSchemas:
    def test_remapping_moves_objects_and_rewrites_their_keys(self):
        inv = inventory(table("cumo-invoicing", "invoice", cols=[col("id", "int4")]))
        remapped = inv.remap_schemas({"cumo-invoicing": "invoicing_qa"})
        assert set(remapped.schemas) == {"invoicing_qa"}
        key = column_key("invoicing_qa", "invoice", "id")
        assert key in remapped.objects
        # The embedded key must move too, or reports would print the old schema.
        assert remapped.objects[key].key == key

    def test_remapping_leaves_unmapped_schemas_alone(self):
        inv = inventory(table("public", "mandant"), table("cumo-invoicing", "invoice"))
        remapped = inv.remap_schemas({"cumo-invoicing": "invoicing_qa"})
        assert table_key("public", "mandant") in remapped.objects

    def test_an_empty_mapping_is_a_no_op(self):
        inv = sample()
        assert inv.remap_schemas({}) is inv


class TestKeys:
    def test_path_includes_the_subname_for_a_column(self):
        assert column_key("public", "invoice", "amount").path == "public.invoice.amount"
        assert table_key("public", "invoice").path == "public.invoice"

    def test_display_names_the_kind(self):
        assert column_key("public", "t", "c").display() == "column public.t.c"
        assert (
            ObjectKey(ObjectKind.ENUM_TYPE, "public", "status").display()
            == "enum type public.status"
        )

    def test_a_column_key_points_at_its_table(self):
        assert column_key("public", "invoice", "amount").parent() == table_key("public", "invoice")
        assert table_key("public", "invoice").parent() is None

    def test_a_key_needs_a_schema_and_a_name(self):
        with pytest.raises(ValueError, match="schema"):
            ObjectKey(ObjectKind.TABLE, "", "t")

    def test_hyphenated_and_uppercase_names_are_preserved(self):
        key = ObjectKey(ObjectKind.TABLE, "cumo-invoicing", "DATABASECHANGELOG")
        assert key.qualified == "cumo-invoicing.DATABASECHANGELOG"


class TestEmptinessIgnoresDefaultExtensions:
    """``plpgsql`` is installed in every PostgreSQL database by default.

    Counting it would make a freshly created database look non-empty, and the "target is empty"
    short circuit — the one thing standing between a reader and thousands of findings on a database
    nothing was ever deployed to — would never fire.
    """

    def _extension(self, name: str):
        from cumo_schema_comparer.model.objects import Extension

        key = ObjectKey(ObjectKind.EXTENSION, "pg_catalog", name)
        return {key: Extension(key=key, version="1.0")}

    def test_a_database_with_only_extensions_counts_as_empty(self):
        assert inventory(self._extension("plpgsql")).is_empty

    def test_one_real_object_makes_it_non_empty(self):
        inv = inventory(self._extension("plpgsql"), table("public", "invoice"))
        assert not inv.is_empty

    def test_a_truly_empty_inventory_is_empty(self):
        assert inventory().is_empty


class TestKindPlurals:
    """Report headings are user-visible, and a naive plural shows.

    "1 indexs" appeared in a real probe run against the RMV stack.
    """

    def test_index_pluralises_correctly(self):
        assert ObjectKind.INDEX.plural == "indexes"

    def test_the_regular_kinds_just_take_an_s(self):
        assert ObjectKind.TABLE.plural == "tables"
        assert ObjectKind.SEQUENCE.plural == "sequences"
        assert ObjectKind.MATVIEW.plural == "matviews"

    def test_a_multi_word_kind_keeps_its_spaces(self):
        assert ObjectKind.ENUM_TYPE.plural == "enum types"
        assert ObjectKind.COMPOSITE_TYPE.plural == "composite types"

    def test_every_kind_has_a_plural_that_is_not_the_singular(self):
        for kind in ObjectKind:
            assert kind.plural != kind.label
            assert not kind.plural.endswith("xs"), kind
