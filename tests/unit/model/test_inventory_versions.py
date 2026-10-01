"""An inventory on disk outlives the build that wrote it.

The gate used to demand an exact match, so bumping the version would have made every captured
inventory and every --baseline file refuse to load.
"""

import pytest

from cumo_schema_comparer.model.inventory import (
    READABLE_SCHEMA_VERSIONS,
    SCHEMA_VERSION,
    Inventory,
)


def test_the_current_version_is_readable():
    assert SCHEMA_VERSION in READABLE_SCHEMA_VERSIONS


def test_every_released_version_stays_readable():
    # Version 1 files exist on disk in the wild. Dropping one is a breaking change that has to be
    # a deliberate edit to this set, not a side effect of a bump.
    assert 1 in READABLE_SCHEMA_VERSIONS


def test_an_unknown_version_is_refused_by_name():
    with pytest.raises(ValueError, match="unsupported inventory schema_version 99"):
        Inventory.from_json_dict({"schema_version": 99})


def test_a_missing_version_is_refused():
    with pytest.raises(ValueError, match="unsupported inventory schema_version None"):
        Inventory.from_json_dict({})
