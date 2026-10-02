"""A routine carries its body text, not only a hash of it.

The hash is what decides equality and stays; the text is what a reader needs in order to see what
changed. Before this, a changed function reported `body_hash: 3af1… -> 9bc2…` and nothing else.
"""

from datetime import datetime

from cumo_schema_comparer.model.inventory import SCHEMA_VERSION, Inventory, SourceInfo
from cumo_schema_comparer.model.keys import ObjectKey
from cumo_schema_comparer.model.kinds import ObjectKind
from cumo_schema_comparer.model.objects import Routine
from cumo_schema_comparer.normalize.routines import body_hash

BODY = "BEGIN\n  RETURN a + b;\nEND"


def a_routine(body: str | None = BODY) -> Routine:
    key = ObjectKey(ObjectKind.ROUTINE, "public", "f_add(integer, integer)")
    return Routine(key=key, language="plpgsql", body=body, body_hash=body_hash(body))


def inventory_with(obj: Routine) -> Inventory:
    return Inventory(
        source=SourceInfo(
            label="prod",
            database="inv",
            user="u",
            server_version_num=150004,
            server_version="15.4",
            encoding="UTF8",
            datcollate="C",
            datctype="C",
            captured_at=datetime(2026, 10, 1),
        ),
        schemas=("public",),
        objects={obj.key: obj},
        changelog=None,
    )


def test_the_body_is_kept():
    assert a_routine().body == BODY


def test_the_hash_is_still_kept():
    assert a_routine().body_hash is not None


def test_the_body_round_trips_through_json():
    payload = inventory_with(a_routine()).to_json_dict()
    assert payload["objects"][0]["attributes"]["body"] == BODY  # written, not just restored
    restored = Inventory.from_json_dict(payload)
    only = next(iter(restored.objects.values()))
    assert only.body == BODY


def test_the_writer_emits_version_2():
    assert inventory_with(a_routine()).to_json_dict()["schema_version"] == 2
    assert SCHEMA_VERSION == 2


def test_a_version_1_inventory_loads_with_no_body():
    """The real upgrade path: an old capture on disk against a new one from the database."""
    payload = inventory_with(a_routine()).to_json_dict()
    payload["schema_version"] = 1
    for obj in payload["objects"]:
        obj["attributes"].pop("body", None)
    only = next(iter(Inventory.from_json_dict(payload).objects.values()))
    assert only.body is None
    assert only.body_hash is not None


def test_body_does_not_participate_in_equality():
    assert a_routine(BODY) == a_routine(None).__class__(
        key=a_routine().key, language="plpgsql", body=None, body_hash=body_hash(BODY)
    )
