"""Constraint and index value objects.

The derived properties here are what the diff engine actually compares, so they carry the meaning:
``referential_actions`` decides whether an ``ON DELETE`` change is reported, and an index's
structural fields decide whether a renamed index is one finding or two.
"""

from __future__ import annotations

import pytest

from db_schema_diff.model.inventory import Inventory
from db_schema_diff.model.keys import ObjectKey
from db_schema_diff.model.kinds import AUTO_NAMED_KINDS, ObjectKind
from db_schema_diff.model.objects import Constraint, Index, IndexKey, RawValues
from tests.support.builders import inventory


def constraint_key(table: str, name: str, schema: str = "acme-invoicing") -> ObjectKey:
    return ObjectKey(ObjectKind.CONSTRAINT, schema, table, name)


def index_key(name: str, schema: str = "acme-invoicing") -> ObjectKey:
    return ObjectKey(ObjectKind.INDEX, schema, name)


class TestConstraintLabels:
    @pytest.mark.parametrize(
        ("contype", "label"),
        [
            ("p", "primary key"),
            ("u", "unique"),
            ("f", "foreign key"),
            ("c", "check"),
            ("x", "exclusion"),
        ],
    )
    def test_each_type_has_a_readable_label(self, contype, label):
        assert Constraint(key=constraint_key("t", "c"), contype=contype).kind_label == label

    def test_an_unknown_type_falls_back_to_its_code(self):
        # Rather than raising: a newer PostgreSQL adding a type must not break a report.
        assert Constraint(key=constraint_key("t", "c"), contype="z").kind_label == "z"


class TestForeignKeyProperties:
    def _fk(self, **overrides):
        defaults = {
            "key": constraint_key("invoice_line", "invoice_line_invoice_id_fkey"),
            "contype": "f",
            "columns": ("invoice_id",),
            "references_schema": "acme-invoicing",
            "references_name": "invoice",
            "references_columns": ("id",),
        }
        return Constraint(**{**defaults, **overrides})

    def test_references_is_qualified_and_names_the_columns(self):
        # Qualified so a target that moved a table between schemas is reported, not accepted.
        assert self._fk().references == "acme-invoicing.invoice(id)"

    def test_references_is_none_for_a_non_foreign_key(self):
        assert Constraint(key=constraint_key("t", "c"), contype="c").references is None

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("a", "NO ACTION"),
            ("r", "RESTRICT"),
            ("c", "CASCADE"),
            ("n", "SET NULL"),
            ("d", "SET DEFAULT"),
        ],
    )
    def test_each_referential_action_is_spelled_out(self, code, expected):
        assert expected in (self._fk(on_delete=code).referential_actions or "")

    def test_an_absent_action_defaults_to_no_action(self):
        # PostgreSQL's own default, and it is not the same as RESTRICT: NO ACTION can be deferred.
        assert self._fk().referential_actions == "ON UPDATE NO ACTION ON DELETE NO ACTION"

    def test_cascade_and_restrict_are_distinguishable(self):
        # The pair that behaves differently the first time a referenced row is deleted.
        assert (
            self._fk(on_delete="c").referential_actions
            != self._fk(on_delete="r").referential_actions
        )

    def test_referential_actions_is_none_for_a_non_foreign_key(self):
        assert Constraint(key=constraint_key("t", "c"), contype="u").referential_actions is None


class TestIndexKeyDisplay:
    def test_a_plain_column_displays_as_its_name(self):
        assert IndexKey("mandant_id").display() == "mandant_id"

    def test_ordering_and_opclass_are_shown(self):
        key = IndexKey("name", opclass="varchar_pattern_ops", descending=True, nulls_first=True)
        assert key.display() == "name varchar_pattern_ops DESC NULLS FIRST"

    def test_a_collation_is_quoted(self):
        assert IndexKey("name", collation="C").display() == 'name COLLATE "C"'


class TestIndexStructure:
    def _index(self, **overrides):
        defaults = {
            "key": index_key("idx_invoice_number"),
            "table": "invoice",
            "is_unique": True,
            "keys": (IndexKey("number"), IndexKey("mandant_id", included=True)),
        }
        return Index(**{**defaults, **overrides})

    def test_key_columns_exclude_include_columns(self):
        index = self._index()
        assert index.key_columns == ("number",)
        assert index.included_columns == ("mandant_id",)

    def test_structure_summarises_everything_except_the_name(self):
        # What "the same index under a different name" means, and what a report shows so a reader
        # can see at a glance whether the name is the only difference.
        assert self._index().structure == "unique btree (number) INCLUDE (mandant_id)"

    def test_structure_includes_a_predicate(self):
        assert "WHERE status = 'OPEN'" in self._index(predicate="status = 'OPEN'").structure

    def test_structure_includes_nulls_not_distinct(self):
        assert "NULLS NOT DISTINCT" in self._index(nulls_not_distinct=True).structure

    def test_a_non_unique_index_says_so(self):
        assert self._index(is_unique=False).structure.startswith("non-unique")

    def test_the_access_method_is_part_of_the_structure(self):
        assert "gin" in self._index(access_method="gin").structure


class TestAutoNamedKinds:
    def test_indexes_constraints_and_triggers_are_auto_named(self):
        # These are the kinds PostgreSQL generates names for, so a name-only difference is
        # reconciled into one finding instead of a missing/extra pair.
        assert {
            ObjectKind.INDEX,
            ObjectKind.CONSTRAINT,
            ObjectKind.TRIGGER,
        } == AUTO_NAMED_KINDS

    def test_tables_and_columns_are_not(self):
        # Nothing generated those names, so a rename is a real removal plus a real addition.
        assert ObjectKind.TABLE not in AUTO_NAMED_KINDS
        assert ObjectKind.COLUMN not in AUTO_NAMED_KINDS


class TestRoundTrip:
    def test_a_constraint_survives_the_json_round_trip(self):
        key = constraint_key("invoice_line", "fk")
        original = Constraint(
            key=key,
            contype="f",
            columns=("invoice_id",),
            references_schema="acme-invoicing",
            references_name="invoice",
            references_columns=("id",),
            on_delete="c",
            deferrable=True,
            validated=False,
            raw=RawValues({"definition": "FOREIGN KEY ..."}),
        )
        restored = Inventory.from_json(inventory({key: original}).to_json())
        assert restored.objects[key] == original
        assert restored.objects[key].raw.get("definition") == "FOREIGN KEY ..."

    def test_an_index_with_nested_keys_survives_the_json_round_trip(self):
        key = index_key("idx_expr")
        original = Index(
            key=key,
            table="invoice",
            keys=(
                IndexKey("lower(number)", is_expression=True, descending=True),
                IndexKey("mandant_id", included=True),
            ),
            predicate="status = 'OPEN'",
        )
        restored = Inventory.from_json(inventory({key: original}).to_json())
        assert restored.objects[key] == original
        assert restored.objects[key].keys[0].is_expression

    def test_an_index_with_no_keys_round_trips_as_an_empty_tuple(self):
        # Guards the nested-tuple decoding against the empty case.
        key = index_key("idx_empty")
        original = Index(key=key, table="t")
        restored = Inventory.from_json(inventory({key: original}).to_json())
        assert restored.objects[key].keys == ()


class TestRoutineProperties:
    def _routine(self, **overrides):
        from db_schema_diff.model.objects import Routine

        key = ObjectKey(ObjectKind.ROUTINE, "acme-invoicing", "f(integer)")
        return Routine(key=key, **overrides)

    @pytest.mark.parametrize(
        ("prokind", "label"),
        [("f", "function"), ("p", "procedure"), ("a", "aggregate"), ("w", "window function")],
    )
    def test_each_routine_kind_has_a_label(self, prokind, label):
        assert self._routine(prokind=prokind).kind_label == label

    @pytest.mark.parametrize(
        ("code", "label"), [("i", "IMMUTABLE"), ("s", "STABLE"), ("v", "VOLATILE")]
    )
    def test_volatility_is_spelled_out(self, code, label):
        assert self._routine(volatility=code).volatility_label == label

    @pytest.mark.parametrize(
        ("code", "label"), [("s", "SAFE"), ("r", "RESTRICTED"), ("u", "UNSAFE")]
    )
    def test_parallel_safety_is_spelled_out(self, code, label):
        assert self._routine(parallel_safety=code).parallel_label == label

    def test_the_signature_names_the_return_type(self):
        assert "RETURNS numeric" in self._routine(return_type="numeric").signature

    def test_an_unknown_code_falls_back_rather_than_raising(self):
        # A newer PostgreSQL adding a code must not break a report.
        assert self._routine(volatility="z").volatility_label == "z"


class TestTriggerProperties:
    def _trigger(self, **overrides):
        from db_schema_diff.model.objects import Trigger

        key = ObjectKey(ObjectKind.TRIGGER, "public", "invoice", "stamp")
        return Trigger(key=key, **overrides)

    def test_the_signature_reads_like_the_ddl(self):
        trigger = self._trigger(timing="AFTER", events=("INSERT", "UPDATE"), level="ROW")
        assert trigger.signature == "AFTER INSERT OR UPDATE FOR EACH ROW"

    def test_a_disabled_trigger_says_so(self):
        assert self._trigger(enabled="D").disabled is True
        assert self._trigger(enabled="O").disabled is False

    def test_a_replica_trigger_is_not_reported_as_disabled(self):
        # 'R' means it fires on a replica, which is not the same as doing nothing.
        assert self._trigger(enabled="R").disabled is False


class TestUserTypeProperties:
    @pytest.mark.parametrize(
        ("typtype", "label"),
        [
            ("e", "enum"),
            ("d", "domain"),
            ("c", "composite type"),
            ("r", "range"),
            ("m", "multirange"),
        ],
    )
    def test_each_type_kind_has_a_label(self, typtype, label):
        from db_schema_diff.model.objects import UserType

        key = ObjectKey(ObjectKind.ENUM_TYPE, "public", "t")
        assert UserType(key=key, typtype=typtype).kind_label == label


class TestViewAndSequenceRoundTrip:
    def test_a_view_survives_the_json_round_trip(self):
        from db_schema_diff.model.inventory import Inventory
        from db_schema_diff.model.objects import View
        from tests.support.builders import inventory

        key = ObjectKey(ObjectKind.VIEW, "acme-invoicing", "invoice_summary")
        original = View(
            key=key,
            columns=("id integer", "number character varying(40)"),
            definition="SELECT ...",
        )
        restored = Inventory.from_json(inventory({key: original}).to_json())
        assert restored.objects[key] == original

    def test_a_sequence_survives_the_json_round_trip(self):
        from db_schema_diff.model.inventory import Inventory
        from db_schema_diff.model.objects import Sequence
        from tests.support.builders import inventory

        key = ObjectKey(ObjectKind.SEQUENCE, "acme-invoicing", "reference_seq")
        original = Sequence(key=key, data_type="int8", start_value=1000, increment=10, cycles=True)
        restored = Inventory.from_json(inventory({key: original}).to_json())
        assert restored.objects[key] == original

    def test_a_trigger_survives_the_json_round_trip(self):
        from db_schema_diff.model.inventory import Inventory
        from db_schema_diff.model.objects import Trigger
        from tests.support.builders import inventory

        key = ObjectKey(ObjectKind.TRIGGER, "public", "invoice", "stamp")
        original = Trigger(
            key=key,
            function="public.stamp",
            events=("INSERT", "UPDATE"),
            arguments=("'a'", "'b'"),
            condition="new.status = 'OPEN'",
        )
        restored = Inventory.from_json(inventory({key: original}).to_json())
        assert restored.objects[key] == original
