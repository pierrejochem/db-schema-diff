"""Same contract for a report: `render --from` must read what an earlier build wrote."""

import pytest

from db_schema_diff.diff.model import (
    REPORT_SCHEMA_VERSION,
    ComparisonReport,
)


def test_the_current_version_is_readable():
    """The reader must accept what the writer produces."""
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "name": "test",
        "master": "master",
        "generated_at": "2024-01-01T00:00:00",
        "tool_version": "1.0.0",
        "fail_on": "error",
        "probe_failed": False,
        "worst_severity": None,
        "notes": [],
        "targets": [],
    }
    report = ComparisonReport.from_json_dict(payload)
    assert report.name == "test"


def test_every_released_version_stays_readable():
    payload = {
        "schema_version": 1,
        "name": "test",
        "master": "master",
        "generated_at": "2024-01-01T00:00:00",
        "tool_version": "1.0.0",
        "fail_on": "error",
        "probe_failed": False,
        "worst_severity": None,
        "notes": [],
        "targets": [],
    }
    report = ComparisonReport.from_json_dict(payload)
    assert report.name == "test"


def test_an_unknown_version_is_refused_by_name():
    with pytest.raises(
        ValueError,
        match=r"unsupported report schema_version 99; this build reads \[1, 2\] and writes 2",
    ):
        ComparisonReport.from_json_dict({"schema_version": 99})


def test_a_missing_version_is_refused():
    with pytest.raises(
        ValueError,
        match=r"unsupported report schema_version None; this build reads \[1, 2\] and writes 2",
    ):
        ComparisonReport.from_json_dict({})


def test_a_float_version_is_refused():
    with pytest.raises(ValueError, match=r"unsupported report schema_version 1\.0"):
        ComparisonReport.from_json_dict({"schema_version": 1.0})


def test_a_bool_version_is_refused():
    with pytest.raises(ValueError, match="unsupported report schema_version True"):
        ComparisonReport.from_json_dict({"schema_version": True})


def test_a_list_version_is_refused():
    with pytest.raises(ValueError, match=r"unsupported report schema_version \[1\]"):
        ComparisonReport.from_json_dict({"schema_version": [1]})


def test_a_dict_version_is_refused():
    with pytest.raises(ValueError, match="unsupported report schema_version"):
        ComparisonReport.from_json_dict({"schema_version": {}})


def test_a_string_version_is_refused_with_hint():
    with pytest.raises(
        ValueError,
        match=r"unsupported report schema_version '1'; .* \(expected an integer\)",
    ):
        ComparisonReport.from_json_dict({"schema_version": "1"})


def test_version_1_loads_when_version_2_is_readable(monkeypatch):
    """An old payload loads when the readable set includes newer versions.

    This is the whole promise: once version 2 is written, we must still read version 1.
    """
    # Create a real version-1 report
    v1_payload = {
        "schema_version": 1,
        "name": "test-report",
        "master": "production",
        "generated_at": "2024-01-01T00:00:00",
        "tool_version": "1.0.0",
        "fail_on": "error",
        "probe_failed": False,
        "worst_severity": None,
        "notes": [],
        "targets": [],
    }

    # Monkeypatch to simulate a future where version 2 exists
    import db_schema_diff.diff.model as model_module

    monkeypatch.setattr(model_module, "READABLE_REPORT_VERSIONS", frozenset({1, 2}))
    monkeypatch.setattr(model_module, "REPORT_SCHEMA_VERSION", 2)

    # The version-1 payload should still load
    report = ComparisonReport.from_json_dict(v1_payload)
    assert report.name == "test-report"
    assert report.master_label == "production"


def test_the_writer_emits_version_2():
    assert REPORT_SCHEMA_VERSION == 2
    from db_schema_diff.diff.model import READABLE_REPORT_VERSIONS

    assert frozenset({1, 2}) == READABLE_REPORT_VERSIONS


def test_a_version_1_report_keeps_its_content():
    payload = {
        "schema_version": 1,
        "name": "old",
        "master": "prod",
        "generated_at": "2024-01-01T00:00:00",
        "tool_version": "1.0.0",
        "fail_on": "error",
        "probe_failed": False,
        "worst_severity": None,
        "notes": [],
        "targets": [],
    }
    report = ComparisonReport.from_json_dict(payload)
    assert (report.name, report.master_label, report.tool_version) == ("old", "prod", "1.0.0")
