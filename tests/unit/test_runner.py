"""Report assembly.

``build_report`` is pure given already-captured inventories, so the interesting behaviour — what
happens when a source could not be inspected — is testable without a database. That matters
because those are exactly the paths hardest to trigger on purpose against a real server.
"""

from __future__ import annotations

import pytest

from cumo_schema_comparer.config.model import ComparerConfig
from cumo_schema_comparer.diff.severity import Severity
from cumo_schema_comparer.errors import ConfigError
from cumo_schema_comparer.runner import CaptureResult, build_report
from tests.support.builders import col, inventory, table

BASE = {
    "version": 1,
    "name": "invoicing",
    "master": {"label": "prod", "dsn_env": "PROD_DSN"},
    "targets": [{"label": "qa", "dsn_env": "QA_DSN"}, {"label": "dev", "dsn_env": "DEV_DSN"}],
}


def config(**overrides) -> ComparerConfig:
    return ComparerConfig.model_validate({**BASE, **overrides})


def ok(label: str, *groups) -> CaptureResult:
    return CaptureResult(label=label, inventory=inventory(*groups, label=label))


def failed(label: str, reason: str = "connection refused") -> CaptureResult:
    return CaptureResult(label=label, error=f"{label}: cannot connect ({reason})")


SCHEMA = table("public", "invoice", cols=[col("id", "int4", nullable=False)])
DRIFTED = table("public", "invoice", cols=[col("id", "int8", nullable=False)])


class TestHappyPath:
    def test_every_target_is_compared_against_the_master(self):
        report = build_report(
            config(),
            {"prod": ok("prod", SCHEMA), "qa": ok("qa", SCHEMA), "dev": ok("dev", DRIFTED)},
        )
        assert [t.target_label for t in report.targets] == ["qa", "dev"]
        assert report.targets[0].findings == ()
        assert report.targets[1].worst_severity() is Severity.ERROR
        assert report.probe_failed is False

    def test_report_metadata_is_carried_through(self):
        report = build_report(config(), {"prod": ok("prod"), "qa": ok("qa"), "dev": ok("dev")})
        assert report.name == "invoicing"
        assert report.master_label == "prod"
        assert report.tool_version
        assert report.generated_at

    def test_the_fail_on_setting_comes_from_the_config(self):
        cfg = config(options={"fail_on": "warning"})
        report = build_report(cfg, {"prod": ok("prod"), "qa": ok("qa"), "dev": ok("dev")})
        assert report.fail_on == "warning"

    def test_only_selected_targets_are_compared(self):
        report = build_report(
            config(),
            {"prod": ok("prod", SCHEMA), "qa": ok("qa", SCHEMA), "dev": ok("dev", DRIFTED)},
            targets=("qa",),
        )
        assert [t.target_label for t in report.targets] == ["qa"]

    def test_an_unknown_target_is_a_config_error_not_a_key_error(self):
        # A mistyped --target must produce the documented exit code, not a traceback.
        with pytest.raises(ConfigError, match="staging"):
            build_report(config(), {"prod": ok("prod")}, targets=("staging",))


class TestUnreachableTarget:
    def _report(self):
        return build_report(
            config(),
            {"prod": ok("prod", SCHEMA), "qa": failed("qa"), "dev": ok("dev", SCHEMA)},
        )

    def test_one_dead_target_does_not_stop_the_others(self):
        report = self._report()
        qa, dev = report.targets
        assert qa.skipped
        assert not dev.skipped
        assert dev.findings == ()

    def test_the_report_records_that_it_is_incomplete(self):
        assert self._report().probe_failed is True

    def test_the_skipped_target_says_why(self):
        assert "cannot connect" in (self._report().targets[0].failed or "")


class TestUnreachableMaster:
    def _report(self):
        return build_report(
            config(), {"prod": failed("prod"), "qa": ok("qa", SCHEMA), "dev": ok("dev", SCHEMA)}
        )

    def test_without_the_reference_every_target_is_skipped(self):
        # There is nothing to compare against, so reporting the targets as "in sync" would be a
        # lie about a comparison that never happened.
        report = self._report()
        assert all(t.skipped for t in report.targets)
        assert report.probe_failed is True

    def test_a_note_explains_that_the_master_failed(self):
        messages = " ".join(n.message for n in self._report().notes)
        assert "master 'prod'" in messages
        assert Severity.ERROR in [n.severity for n in self._report().notes]


class TestConfigWarnings:
    def test_a_target_sharing_the_masters_credential_is_reported(self):
        cfg = config(targets=[{"label": "qa", "dsn_env": "PROD_DSN"}])
        report = build_report(cfg, {"prod": ok("prod", SCHEMA), "qa": ok("qa", SCHEMA)})
        assert any("PROD_DSN" in n.message for n in report.notes)


class TestSchemaMap:
    def test_the_config_map_is_inverted_before_comparison(self):
        """The config is written master-name-first, because that is how a person thinks.

        Comparison happens in the master's namespace, so the map has to be inverted on the way in.
        Getting this backwards makes every object look missing *and* extra, so it is worth pinning.
        """
        cfg = config(
            targets=[
                {
                    "label": "qa",
                    "dsn_env": "QA_DSN",
                    "schema_map": {"cumo-invoicing": "invoicing_qa"},
                }
            ]
        )
        master = CaptureResult(
            label="prod",
            inventory=inventory(
                table("cumo-invoicing", "invoice", cols=[col("id", "int4")]), label="prod"
            ),
        )
        target = CaptureResult(
            label="qa",
            inventory=inventory(
                table("invoicing_qa", "invoice", cols=[col("id", "int4")]), label="qa"
            ),
        )
        report = build_report(cfg, {"prod": master, "qa": target})
        assert report.targets[0].findings == ()
