"""Secrets embedded in definition text must not reach a report by default.

The original plan promised this flag and it was never built, while view definitions, check
expressions and column defaults already travelled into the HTML report that CI uploads.
"""

from __future__ import annotations

import inspect

from click.testing import CliRunner

from cumo_schema_comparer import build as build_module
from cumo_schema_comparer import runner
from cumo_schema_comparer.build import (
    _routines,
    build_inventory,
    masked_fields,
    redact_inventory,
)
from cumo_schema_comparer.cli import cli
from cumo_schema_comparer.config.model import SourceRef
from cumo_schema_comparer.diff.attributes import SPECS
from cumo_schema_comparer.model.keys import ObjectKey
from cumo_schema_comparer.model.kinds import ObjectKind
from cumo_schema_comparer.model.objects import Index, IndexKey, RawValues, Routine, UserType
from tests.support.builders import col, inventory, table

#: The definition-bearing attributes. The same ``body=True`` flag drives masking, the choice of a
#: diff over a one-line row, and the PG-major-skew severity downgrade, so a spec flipped by
#: accident changes all three at once. Pinned so that is a deliberate edit.
EXPECTED_BODY_SPECS = frozenset(
    {
        "column.default",
        "column.generated",
        "composite_type.constraints",
        "composite_type.default",
        "constraint.expression",
        "domain_type.constraints",
        "domain_type.default",
        "enum_type.constraints",
        "enum_type.default",
        "index.predicate",
        "matview.definition",
        "range_type.constraints",
        "range_type.default",
        "routine.argument_defaults",
        "routine.body_hash",
        "table.partition_bound",
        "table.partition_key",
        "trigger.condition",
        "view.definition",
    }
)


def test_a_default_carrying_a_connection_string_is_masked(an_inventory_with_secret_default):
    masked = redact_inventory(an_inventory_with_secret_default)
    text = masked.to_json()
    assert "s3cret" not in text
    assert "***:" in text


def test_an_ordinary_default_is_untouched(an_inventory_with_plain_default):
    masked = redact_inventory(an_inventory_with_plain_default)
    assert "'paid'" in masked.to_json()
    assert masked.to_json() == an_inventory_with_plain_default.to_json()


def test_only_definition_fields_are_touched(an_inventory_with_secret_default):
    # Identifiers, types and flags are never rewritten: masking a column name would make the
    # inventory unusable.
    before = an_inventory_with_secret_default
    after = redact_inventory(before)
    assert list(after.objects) == list(before.objects)
    for key, obj in after.objects.items():
        original = before.objects[key]
        for name in ("key", "data_type", "ordinal", "is_nullable", "persistence"):
            if hasattr(obj, name):
                assert getattr(obj, name) == getattr(original, name)
    assert "public" in after.to_json()


def test_redaction_is_idempotent(an_inventory_with_secret_default):
    once = redact_inventory(an_inventory_with_secret_default)
    assert redact_inventory(once).to_json() == once.to_json()


def test_the_same_secret_masks_identically_and_a_different_one_differently():
    def masked(url):
        inv = inventory(table("public", "t", cols=[col("c", default=f"'{url}'::text")]))
        return redact_inventory(inv).to_json()

    assert masked("postgresql://u:one@h/db") == masked("postgresql://u:one@h/db")
    assert masked("postgresql://u:one@h/db") != masked("postgresql://u:two@h/db")


def test_the_set_of_body_specs_is_pinned():
    actual = {spec.name for specs in SPECS.values() for spec in specs if spec.body}
    assert actual == EXPECTED_BODY_SPECS


#: Every field that is masked, per kind: the fields derived from the body specs and the explicit
#: extras together. Equality, not containment, so deleting an entry or adding a spurious one fails.
EXPECTED_MASKED_FIELDS = {
    ObjectKind.TABLE: ("partition_key", "partition_bound"),
    ObjectKind.COLUMN: ("generated", "default"),
    ObjectKind.CONSTRAINT: ("expression",),
    ObjectKind.INDEX: ("predicate", "keys"),
    ObjectKind.VIEW: ("definition",),
    ObjectKind.MATVIEW: ("definition",),
    ObjectKind.ROUTINE: ("body", "argument_defaults", "arguments", "config"),
    ObjectKind.TRIGGER: ("condition", "arguments"),
    ObjectKind.ENUM_TYPE: ("constraints", "default", "labels"),
    ObjectKind.DOMAIN_TYPE: ("constraints", "default"),
    ObjectKind.COMPOSITE_TYPE: ("constraints", "default"),
    ObjectKind.RANGE_TYPE: ("constraints", "default"),
}


def test_the_masked_fields_are_exactly_the_pinned_set():
    actual = {kind: set(names) for kind, names in masked_fields().items()}
    assert actual == {kind: set(names) for kind, names in EXPECTED_MASKED_FIELDS.items()}


def test_every_masked_field_exists_on_its_object():
    from dataclasses import fields

    from cumo_schema_comparer.model import objects as m

    classes = {
        ObjectKind.TABLE: m.Table,
        ObjectKind.COLUMN: m.Column,
        ObjectKind.CONSTRAINT: m.Constraint,
        ObjectKind.INDEX: m.Index,
        ObjectKind.VIEW: m.View,
        ObjectKind.MATVIEW: m.View,
        ObjectKind.ROUTINE: m.Routine,
        ObjectKind.TRIGGER: m.Trigger,
    }
    for kind, names in masked_fields().items():
        cls = classes.get(kind, m.UserType)
        assert set(names) <= {f.name for f in fields(cls)}, kind


def test_an_expression_index_key_is_masked():
    key = IndexKey(expression="(number || 'postgresql://u:s3cretXXX@h/db')", is_expression=True)
    k = ObjectKey(ObjectKind.INDEX, "public", "ix")
    idx = Index(
        key=k,
        table="t",
        keys=(key,),
        predicate=None,
        raw=RawValues(
            {"definition": "CREATE INDEX ix ON t ((number || 'postgresql://u:s3cretXXX@h/db'))"}
        ),
    )
    masked = redact_inventory(inventory({k: idx})).objects[k]
    assert "s3cret" not in repr(masked.keys)
    assert "s3cret" not in masked.structure
    assert masked.keys[0].is_expression is True


def test_an_enum_label_carrying_a_connection_string_is_masked():
    """The eighth leak path. A label is free text a user wrote, and the only one left.

    Every other unmasked attribute of every kind is an identifier, a type name, a flag or an
    enumeration; reloptions look like a setting but PostgreSQL rejects any parameter it does not
    recognise, and every one it recognises is a boolean, an integer or an enum.
    """
    k = ObjectKey(ObjectKind.ENUM_TYPE, "public", "sync_mode")
    enum = UserType(key=k, typtype="e", labels=("ok", "postgresql://u:s3cretXXX@h/db"))
    masked = redact_inventory(inventory({k: enum})).objects[k]

    assert masked.labels[0] == "ok", "an ordinary label must survive"
    assert masked.labels[1].startswith("***:")
    assert "s3cret" not in redact_inventory(inventory({k: enum})).to_json()
    # The order is the type's comparison operator, so it must not move.
    assert len(masked.labels) == 2


def test_a_masked_label_still_compares_the_same_way():
    # Masking is stable, so an unchanged credential is not drift and a rotated one still is.
    def labels(secret):
        k = ObjectKey(ObjectKind.ENUM_TYPE, "public", "e")
        enum = UserType(key=k, typtype="e", labels=(f"postgresql://u:{secret}@h/db",))
        return redact_inventory(inventory({k: enum})).objects[k].labels

    assert labels("one") == labels("one")
    assert labels("one") != labels("two")


def test_an_apostrophe_in_a_label_survives_the_quoting():
    # The label is judged as the literal it was written as, so it has to be quoted and unquoted
    # again; a naive round trip doubles or eats the apostrophe.
    k = ObjectKey(ObjectKind.ENUM_TYPE, "public", "e")
    enum = UserType(key=k, typtype="e", labels=("it's fine",))
    assert redact_inventory(inventory({k: enum})).objects[k].labels == ("it's fine",)


def test_a_routine_config_setting_is_masked():
    k = ObjectKey(ObjectKind.ROUTINE, "public", "f()")
    routine = Routine(key=k, config=("app.dsn=postgresql://u:s3cretXXX@h/db",))
    masked = redact_inventory(inventory({k: routine})).objects[k]
    assert "s3cret" not in repr(masked.config)


def test_redact_literals_defaults_to_on_everywhere():
    for fn in (build_inventory, runner.capture, runner.capture_all, runner.compare):
        assert inspect.signature(fn).parameters["redact_literals"].default is True, fn.__name__


class _FakeIntrospector:
    def __init__(self, connection, features):
        pass

    def server_info(self):
        return {
            "database": "d",
            "user": "u",
            "server_version_num": 150004,
            "server_version": "15.4",
            "encoding": "UTF8",
            "datcollate": "c",
            "datctype": "c",
        }

    def schemas(self, **kwargs):
        return []


def test_build_inventory_applies_redaction(monkeypatch):
    calls = []
    monkeypatch.setattr(build_module, "server_features", lambda connection: None)
    monkeypatch.setattr(build_module, "Introspector", _FakeIntrospector)
    monkeypatch.setattr(build_module, "redact_inventory", lambda inv: calls.append(inv) or inv)
    source = SourceRef(label="x", dsn_env="X")
    build_inventory(None, source, skip_liquibase=True)
    assert len(calls) == 1
    build_inventory(None, source, skip_liquibase=True, redact_literals=False)
    assert len(calls) == 1


SECRET_BODY = "BEGIN\n  PERFORM dblink_connect('postgresql://u:s3cret@h/db');\n  RETURN 1;\nEND"


def _routine_inventory():
    row = {
        "schema": "public",
        "name": "f",
        "identity_arguments": "",
        "language": "plpgsql",
        "body": SECRET_BODY,
    }
    return inventory(_routines([row]))


def test_a_secret_in_a_routine_body_is_gone_from_body_and_raw():
    original = _routine_inventory()
    routine = next(iter(original.objects.values()))
    assert "s3cret" in routine.body
    assert "s3cret" in routine.raw.values["body"]

    masked = next(iter(redact_inventory(original).objects.values()))
    assert "s3cret" not in masked.body
    assert "s3cret" not in masked.raw.values["body"]
    assert "dblink_connect" in masked.body  # the code around the literal survives
    assert "s3cret" not in redact_inventory(original).to_json()


def test_redaction_does_not_change_which_routines_compare_as_different():
    original = next(iter(_routine_inventory().objects.values()))
    masked = next(iter(redact_inventory(_routine_inventory()).objects.values()))
    assert masked.body_hash == original.body_hash
    assert masked == original


def test_a_raw_value_is_masked_even_when_the_field_is_clean():
    inv = inventory(
        table(
            "public",
            "t",
            cols=[col("c", default="1", raw={"default": "'postgresql://u:s3cret@h/d'"})],
        )
    )
    assert "s3cret" not in redact_inventory(inv).to_json()
    assert RawValues({"a": "b"}).values == {"a": "b"}


def test_the_flag_defaults_on_and_says_why():
    for command in ("compare", "inventory"):
        result = CliRunner().invoke(cli, [command, "--help"])
        assert result.exit_code == 0
        assert "--redact-literals" in result.output
        assert "--no-redact-literals" in result.output
        flat = " ".join(result.output.split())
        assert "On by default" in flat
        assert "no longer safe to share" in flat
