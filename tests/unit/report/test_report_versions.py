"""Same contract for a report: `render --from` must read what an earlier build wrote."""

import pytest

from cumo_schema_comparer.diff.model import (
    READABLE_REPORT_VERSIONS,
    REPORT_SCHEMA_VERSION,
    ComparisonReport,
)


def test_the_current_version_is_readable():
    assert REPORT_SCHEMA_VERSION in READABLE_REPORT_VERSIONS


def test_every_released_version_stays_readable():
    assert 1 in READABLE_REPORT_VERSIONS


def test_an_unknown_version_is_refused_by_name():
    with pytest.raises(ValueError, match="unsupported report schema_version 99"):
        ComparisonReport.from_json_dict({"schema_version": 99})
