"""Secrets embedded in definition text must not reach a report by default.

The original plan promised this flag and it was never built, while view definitions, check
expressions and column defaults already travelled into the HTML report that CI uploads.
"""

from __future__ import annotations

from click.testing import CliRunner

from cumo_schema_comparer.build import _routines, masked_fields, redact_inventory
from cumo_schema_comparer.cli import cli
from cumo_schema_comparer.diff.attributes import SPECS
from cumo_schema_comparer.model.kinds import ObjectKind
from cumo_schema_comparer.model.objects import RawValues
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


def test_the_masked_fields_are_derived_from_the_body_specs():
    fields = masked_fields()
    derived = {f"{kind.value}.{name}" for kind, names in fields.items() for name in names}
    for spec_name in EXPECTED_BODY_SPECS - {"routine.body_hash"}:
        assert spec_name in derived, spec_name
    # body_hash holds a digest; the text that travels is Routine.body, mapped deliberately.
    assert "routine.body_hash" not in derived
    assert "body" in fields[ObjectKind.ROUTINE]
    # Every masked name is a real field of the object it is listed for.
    sample = _routines([{"schema": "s", "name": "f", "identity_arguments": "", "language": "sql"}])
    routine = next(iter(sample.values()))
    for name in fields[ObjectKind.ROUTINE]:
        assert hasattr(routine, name)


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
