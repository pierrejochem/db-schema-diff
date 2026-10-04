"""A renderer can tell a printed definition from a scalar without consulting the specs."""

import pytest

from db_schema_diff.diff.engine import diff_inventories
from db_schema_diff.diff.model import AttributeDelta
from db_schema_diff.diff.severity import Severity
from tests.support.builders import col, inventory, routine, table


def test_the_flag_defaults_to_false():
    assert AttributeDelta("column.type", "int4", "int8", Severity.ERROR).body is False


def test_a_true_flag_round_trips():
    delta = AttributeDelta("view.definition", "SELECT 1", "SELECT 2", Severity.WARNING, body=True)
    assert delta.to_json_dict()["body"] is True
    assert AttributeDelta.from_json_dict(delta.to_json_dict()).body is True


def test_a_false_flag_is_omitted_from_json():
    # An existing report's JSON must not grow a key for every scalar delta.
    assert "body" not in AttributeDelta("column.type", "a", "b", Severity.ERROR).to_json_dict()


def test_every_body_spec_is_reachable():
    from db_schema_diff.diff.attributes import SPECS

    body_specs = [spec.name for specs in SPECS.values() for spec in specs if spec.body]
    assert "routine.body_hash" in body_specs
    assert len(body_specs) > 1, "the flag exists so a renderer can find all of them"


def _deltas(master_objects, target_objects):
    result = diff_inventories(
        inventory(*master_objects, label="prod"), inventory(*target_objects, label="qa")
    )
    return [d for f in result.findings for d in f.deltas]


def test_the_engine_flags_a_body_spec_delta():
    sig = "f_add(integer, integer)"
    (delta,) = _deltas(
        [routine("public", sig, "BEGIN RETURN a + b; END")],
        [routine("public", sig, "BEGIN RETURN a - b; END")],
    )
    assert delta.attribute == "routine.body_hash"
    assert delta.body is True


def test_the_engine_leaves_a_scalar_spec_delta_unflagged():
    (delta,) = _deltas(
        [table("public", "t", cols=[col("a", "integer")])],
        [table("public", "t", cols=[col("a", "bigint")])],
    )
    assert delta.body is False


@pytest.mark.parametrize("payload_extra", [{"body": False}, {"body": 1}, {"body": "true"}, {}])
def test_only_a_literal_true_reads_back_as_a_body(payload_extra):
    data = {"attribute": "x", "master": "a", "target": "b", "severity": "error", **payload_extra}
    assert AttributeDelta.from_json_dict(data).body is False


# --- the display pair: both or neither, and an empty string is a value -------------------------

BASE = {"attribute": "routine.body_hash", "master": "h1", "target": "h2", "severity": "warning"}


def load(**extra):
    return AttributeDelta.from_json_dict({**BASE, **extra})


def test_both_absent_loads_as_none():
    delta = load()
    assert (delta.master_display, delta.target_display) == (None, None)


def test_both_present_is_kept():
    delta = load(master_display="a", target_display="b")
    assert (delta.master_display, delta.target_display) == ("a", "b")


def test_both_null_loads_as_none():
    delta = load(master_display=None, target_display=None)
    assert (delta.master_display, delta.target_display) == (None, None)


def test_only_master_is_normalised_to_neither():
    delta = load(master_display="a")
    assert (delta.master_display, delta.target_display) == (None, None)


def test_only_target_is_normalised_to_neither():
    delta = load(target_display="b")
    assert (delta.master_display, delta.target_display) == (None, None)


def test_one_string_and_one_null_is_normalised_to_neither():
    delta = load(master_display="a", target_display=None)
    assert (delta.master_display, delta.target_display) == (None, None)


@pytest.mark.parametrize("bad", [5, ["x"], {"k": 1}, True])
def test_a_non_string_is_normalised_to_neither(bad):
    for extra in (
        {"master_display": bad, "target_display": "b"},
        {"master_display": "a", "target_display": bad},
        {"master_display": bad, "target_display": bad},
    ):
        delta = load(**extra)
        assert (delta.master_display, delta.target_display) == (None, None)


def test_an_empty_string_pair_round_trips():
    delta = AttributeDelta(
        "routine.body_hash", "h1", "h2", Severity.WARNING, master_display="", target_display=""
    )
    payload = delta.to_json_dict()
    assert payload["master_display"] == "" and payload["target_display"] == ""
    again = AttributeDelta.from_json_dict(payload)
    assert (again.master_display, again.target_display) == ("", "")
