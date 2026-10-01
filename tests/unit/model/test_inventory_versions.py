"""An inventory on disk outlives the build that wrote it.

The gate used to demand an exact match, so bumping the version would have made every captured
inventory and every --baseline file refuse to load.
"""

import pytest

from cumo_schema_comparer.model.inventory import (
    SCHEMA_VERSION,
    Inventory,
)


def test_the_current_version_is_readable():
    """The reader must accept what the writer produces."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "label": "test",
            "host": None,
            "database": "testdb",
            "user": "testuser",
            "server_version_num": 150000,
            "server_version": "15.0",
            "encoding": "UTF8",
            "datcollate": "en_US.UTF-8",
            "datctype": "en_US.UTF-8",
            "captured_at": "2024-01-01T00:00:00",
            "notes": [],
        },
        "schemas": ["public"],
        "objects": [],
        "changelog": None,
    }
    inv = Inventory.from_json_dict(payload)
    assert inv.source.label == "test"


def test_every_released_version_stays_readable():
    # Version 1 files exist on disk in the wild. Dropping one is a breaking change that has to be
    # a deliberate edit to this set, not a side effect of a bump.
    payload = {
        "schema_version": 1,
        "source": {
            "label": "test",
            "host": None,
            "database": "testdb",
            "user": "testuser",
            "server_version_num": 150000,
            "server_version": "15.0",
            "encoding": "UTF8",
            "datcollate": "en_US.UTF-8",
            "datctype": "en_US.UTF-8",
            "captured_at": "2024-01-01T00:00:00",
            "notes": [],
        },
        "schemas": ["public"],
        "objects": [],
        "changelog": None,
    }
    inv = Inventory.from_json_dict(payload)
    assert inv.source.label == "test"


def test_an_unknown_version_is_refused_by_name():
    with pytest.raises(
        ValueError,
        match=r"unsupported inventory schema_version 99; this build reads \[1\] and writes 1",
    ):
        Inventory.from_json_dict({"schema_version": 99})


def test_a_missing_version_is_refused():
    with pytest.raises(
        ValueError,
        match=r"unsupported inventory schema_version None; this build reads \[1\] and writes 1",
    ):
        Inventory.from_json_dict({})


def test_a_float_version_is_refused():
    with pytest.raises(ValueError, match=r"unsupported inventory schema_version 1\.0"):
        Inventory.from_json_dict({"schema_version": 1.0})


def test_a_bool_version_is_refused():
    with pytest.raises(ValueError, match="unsupported inventory schema_version True"):
        Inventory.from_json_dict({"schema_version": True})


def test_a_list_version_is_refused():
    with pytest.raises(ValueError, match=r"unsupported inventory schema_version \[1\]"):
        Inventory.from_json_dict({"schema_version": [1]})


def test_a_dict_version_is_refused():
    with pytest.raises(ValueError, match="unsupported inventory schema_version"):
        Inventory.from_json_dict({"schema_version": {}})


def test_a_string_version_is_refused_with_hint():
    with pytest.raises(
        ValueError,
        match=r"unsupported inventory schema_version '1'; .* \(expected an integer\)",
    ):
        Inventory.from_json_dict({"schema_version": "1"})


def test_version_1_loads_when_version_2_is_readable(monkeypatch):
    """An old payload loads when the readable set includes newer versions.

    This is the whole promise: once version 2 is written, we must still read version 1.
    """
    # Create a real version-1 inventory
    v1_payload = {
        "schema_version": 1,
        "source": {
            "label": "test",
            "host": None,
            "database": "testdb",
            "user": "testuser",
            "server_version_num": 150000,
            "server_version": "15.0",
            "encoding": "UTF8",
            "datcollate": "en_US.UTF-8",
            "datctype": "en_US.UTF-8",
            "captured_at": "2024-01-01T00:00:00",
            "notes": [],
        },
        "schemas": ["public"],
        "objects": [],
        "changelog": None,
    }

    # Monkeypatch to simulate a future where version 2 exists
    import cumo_schema_comparer.model.inventory as inventory_module

    monkeypatch.setattr(inventory_module, "READABLE_SCHEMA_VERSIONS", frozenset({1, 2}))
    monkeypatch.setattr(inventory_module, "SCHEMA_VERSION", 2)

    # The version-1 payload should still load
    inv = Inventory.from_json_dict(v1_payload)
    assert inv.source.label == "test"
    assert inv.source.database == "testdb"
