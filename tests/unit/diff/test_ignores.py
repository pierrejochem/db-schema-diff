"""The ignore ruleset.

A drift detector with no way to say "yes, we know" gets abandoned after its first real run. A
suppression nobody can see is worse than none at all. Both halves of that are tested here: what the
rules match, and that a suppressed finding stays on the record.
"""

from __future__ import annotations

import pytest

from db_schema_comparer.config.model import IgnoreConfig, IgnoreRule
from db_schema_comparer.diff.ignores import (
    Action,
    IgnoreRuleSet,
    load_default_ignores,
)
from db_schema_comparer.diff.model import ObjectStatus
from db_schema_comparer.diff.severity import Severity
from db_schema_comparer.model.keys import ObjectKey, column_key, table_key
from db_schema_comparer.model.kinds import ObjectKind


def ruleset(*rules: dict, case_insensitive: bool = True) -> IgnoreRuleSet:
    return IgnoreRuleSet(
        IgnoreConfig.model_validate(
            {
                "version": 1,
                "options": {"case_insensitive_globs": case_insensitive},
                "rules": list(rules),
            }
        )
    )


def decide(rules: IgnoreRuleSet, key: ObjectKey, **kwargs):
    return rules.for_object(
        target=kwargs.get("target", "qa"),
        key=key,
        status=kwargs.get("status", ObjectStatus.EXTRA_IN_TARGET),
    )


class TestNameMatching:
    def test_a_glob_matches_a_qualified_name(self):
        rules = ruleset({"id": "r", "names": ["public.tmp_*"]})
        assert decide(rules, table_key("public", "tmp_debug")).suppressed
        assert not decide(rules, table_key("public", "invoice")).suppressed

    def test_a_schema_wildcard_matches_any_schema(self):
        rules = ruleset({"id": "r", "names": ["*.tmp_*"]})
        assert decide(rules, table_key("acme-invoicing", "tmp_x")).suppressed

    def test_a_schema_glob_matches_a_whole_schema(self):
        rules = ruleset({"id": "r", "names": ["quartz.*"]})
        assert decide(rules, table_key("quartz", "QRTZ_LOCKS")).suppressed
        assert not decide(rules, table_key("public", "QRTZ_LOCKS")).suppressed

    def test_a_column_is_matched_by_its_full_path(self):
        rules = ruleset({"id": "r", "names": ["*.qrtz_*"]})
        # Both schema.name and schema.name.subname are tried, so one rule covers a table and its
        # columns without the author writing two.
        assert decide(rules, column_key("public", "qrtz_locks", "lock_name")).suppressed

    def test_globs_are_case_insensitive_by_default(self):
        # A rule's author should not have to know whether Liquibase wrote QRTZ_ or qrtz_ here.
        rules = ruleset({"id": "r", "names": ["*.qrtz_*"]})
        assert decide(rules, table_key("public", "QRTZ_LOCKS")).suppressed

    def test_case_sensitivity_can_be_required(self):
        rules = ruleset({"id": "r", "names": ["*.qrtz_*"]}, case_insensitive=False)
        assert not decide(rules, table_key("public", "QRTZ_LOCKS")).suppressed
        assert decide(rules, table_key("public", "qrtz_locks")).suppressed


class TestScopeDimensions:
    def test_a_kind_restriction_is_honoured(self):
        rules = ruleset({"id": "r", "kinds": ["index"], "names": ["*"]})
        assert decide(rules, ObjectKey(ObjectKind.INDEX, "public", "i")).suppressed
        assert not decide(rules, table_key("public", "t")).suppressed

    def test_a_target_restriction_is_honoured(self):
        rules = ruleset({"id": "r", "names": ["*"], "targets": ["dev", "local"]})
        assert decide(rules, table_key("public", "t"), target="dev").suppressed
        assert not decide(rules, table_key("public", "t"), target="prod-mirror").suppressed

    def test_a_status_restriction_separates_extra_from_missing(self):
        """The dimension that makes the ruleset safe to use.

        "Tolerate an extra table in dev" must not also mean "tolerate a missing one", or a ruleset
        written to quieten a scratch table would hide a failed deployment.
        """
        rules = ruleset({"id": "r", "names": ["*"], "statuses": ["extra_in_target"]})
        assert decide(
            rules, table_key("public", "t"), status=ObjectStatus.EXTRA_IN_TARGET
        ).suppressed
        assert not decide(
            rules, table_key("public", "t"), status=ObjectStatus.MISSING_IN_TARGET
        ).suppressed

    def test_dimensions_combine_conjunctively(self):
        rules = ruleset({"id": "r", "kinds": ["index"], "names": ["public.*"], "targets": ["dev"]})
        index = ObjectKey(ObjectKind.INDEX, "public", "i")
        assert decide(rules, index, target="dev").suppressed
        assert not decide(rules, index, target="qa").suppressed
        assert not decide(rules, ObjectKey(ObjectKind.INDEX, "other", "i"), target="dev").suppressed


class TestActions:
    def test_ignore_suppresses(self):
        rules = ruleset({"id": "r", "names": ["*"]})
        assert decide(rules, table_key("public", "t")).action is Action.IGNORE

    def test_warn_clamps_instead_of_hiding(self):
        rules = ruleset({"id": "r", "names": ["*"], "action": "warn"})
        decision = decide(rules, table_key("public", "t"))
        assert decision.clamped
        assert decision.action.clamps_to is Severity.WARNING

    def test_info_clamps_further(self):
        rules = ruleset({"id": "r", "names": ["*"], "action": "info"})
        assert decide(rules, table_key("public", "t")).action.clamps_to is Severity.INFO

    def test_no_match_keeps_the_finding(self):
        rules = ruleset({"id": "r", "names": ["public.nothing"]})
        decision = decide(rules, table_key("public", "t"))
        assert decision.action is Action.KEEP
        assert decision.rule_id is None

    def test_the_matching_rule_id_is_reported(self):
        rules = ruleset({"id": "quartz-runtime", "names": ["*.qrtz_*"]})
        assert decide(rules, table_key("public", "qrtz_locks")).rule_id == "quartz-runtime"

    def test_the_first_matching_rule_wins(self):
        rules = ruleset(
            {"id": "first", "names": ["public.t"], "action": "warn"},
            {"id": "second", "names": ["public.t"]},
        )
        assert decide(rules, table_key("public", "t")).rule_id == "first"


class TestScopeSeparation:
    """A rule naming attributes applies to differences; one without applies to whole objects.

    Conflating them would make "a different collation is fine" also mean "a missing column is fine".
    """

    def test_an_attribute_rule_does_not_suppress_a_whole_object(self):
        rules = ruleset({"id": "r", "attributes": ["column.collation"]})
        assert not decide(
            rules, column_key("public", "t", "c"), status=ObjectStatus.MISSING_IN_TARGET
        ).suppressed

    def test_an_object_rule_does_not_suppress_an_individual_attribute(self):
        rules = ruleset({"id": "r", "names": ["public.t"], "statuses": ["extra_in_target"]})
        decision = rules.for_attribute(
            target="qa",
            key=column_key("public", "t", "c"),
            status=ObjectStatus.DIFFERS,
            attribute="column.collation",
        )
        assert decision.action is Action.KEEP

    def test_an_attribute_rule_matches_its_attribute(self):
        rules = ruleset({"id": "r", "attributes": ["column.collation"]})
        decision = rules.for_attribute(
            target="qa",
            key=column_key("public", "t", "c"),
            status=ObjectStatus.DIFFERS,
            attribute="column.collation",
        )
        assert decision.suppressed

    def test_an_attribute_rule_ignores_other_attributes(self):
        rules = ruleset({"id": "r", "attributes": ["column.collation"]})
        decision = rules.for_attribute(
            target="qa",
            key=column_key("public", "t", "c"),
            status=ObjectStatus.DIFFERS,
            attribute="column.data_type",
        )
        assert decision.action is Action.KEEP


class TestValidation:
    def test_an_unbounded_rule_is_refused(self):
        # A rule restricting nothing would suppress the entire report.
        with pytest.raises(ValueError, match="every finding"):
            IgnoreRule.model_validate({"id": "everything"})

    def test_an_unknown_kind_is_refused(self):
        with pytest.raises(ValueError, match="unknown object kind"):
            IgnoreRule.model_validate({"id": "r", "kinds": ["tabel"]})

    def test_an_unknown_status_is_refused(self):
        with pytest.raises(ValueError, match="unknown status"):
            IgnoreRule.model_validate({"id": "r", "statuses": ["missing"]})

    def test_a_typo_in_a_key_is_refused(self):
        with pytest.raises(ValueError):
            IgnoreRule.model_validate({"id": "r", "name": ["public.t"]})

    def test_duplicate_rule_ids_are_refused(self):
        with pytest.raises(ValueError, match="duplicate ignore rule id"):
            IgnoreConfig.model_validate(
                {
                    "version": 1,
                    "rules": [
                        {"id": "same", "names": ["a"]},
                        {"id": "same", "names": ["b"]},
                    ],
                }
            )

    def test_an_unknown_action_is_refused(self):
        with pytest.raises(ValueError):
            IgnoreRule.model_validate({"id": "r", "names": ["*"], "action": "delete"})


class TestLayering:
    def test_a_project_rule_is_added_to_the_defaults(self):
        rules = IgnoreRuleSet.from_config(
            IgnoreConfig.model_validate(
                {"version": 1, "rules": [{"id": "mine", "names": ["public.scratch"]}]}
            ),
            defaults=load_default_ignores(),
        )
        assert "mine" in rules.rule_ids
        assert "quartz-runtime" in rules.rule_ids

    def test_a_project_rule_overrides_a_default_of_the_same_id(self):
        # Lets a project change one default without restating the rest.
        rules = IgnoreRuleSet.from_config(
            IgnoreConfig.model_validate(
                {
                    "version": 1,
                    "rules": [{"id": "quartz-runtime", "names": ["nothing.at.all"]}],
                }
            ),
            defaults=load_default_ignores(),
        )
        assert not decide(rules, table_key("quartz", "QRTZ_LOCKS")).suppressed

    def test_the_defaults_can_be_turned_off_entirely(self):
        rules = IgnoreRuleSet.from_config(None, defaults=None)
        assert len(rules) == 0
        assert not decide(rules, table_key("quartz", "QRTZ_LOCKS")).suppressed

    def test_an_empty_ruleset_suppresses_nothing(self):
        assert len(IgnoreRuleSet.empty()) == 0


class TestBundledDefaults:
    @pytest.fixture
    def rules(self):
        return IgnoreRuleSet.from_config(None, defaults=load_default_ignores())

    def test_quartz_state_is_suppressed(self):
        rules = IgnoreRuleSet.from_config(None, defaults=load_default_ignores())
        assert decide(rules, table_key("quartz", "locks")).suppressed
        assert decide(rules, table_key("public", "QRTZ_LOCKS")).suppressed

    def test_liquibase_bookkeeping_is_suppressed(self, rules):
        # Its contents are compared far more usefully by the changelog comparison.
        assert decide(rules, table_key("acme-invoicing", "DATABASECHANGELOG")).suppressed
        assert decide(rules, table_key("acme-invoicing", "DATABASECHANGELOGLOCK")).suppressed

    def test_a_scratch_table_is_downgraded_not_hidden(self, rules):
        # Still visible, just not a reason to fail a build.
        decision = decide(rules, table_key("public", "tmp_debug"))
        assert decision.clamped
        assert decision.rule_id == "scratch-objects"

    def test_a_missing_scratch_table_is_not_downgraded(self, rules):
        # The scratch rule is scoped to extra_in_target, so it cannot excuse a missing object.
        assert (
            decide(
                rules, table_key("public", "tmp_debug"), status=ObjectStatus.MISSING_IN_TARGET
            ).action
            is Action.KEEP
        )

    def test_real_application_objects_are_untouched(self, rules):
        assert decide(rules, table_key("acme-invoicing", "invoice")).action is Action.KEEP
        assert (
            decide(rules, column_key("acme-invoicing", "invoice", "number")).action is Action.KEEP
        )

    def test_every_default_rule_explains_itself(self):
        # A default that hides drift without saying why is not reviewable.
        for rule in load_default_ignores().rules:
            assert rule.reason, rule.id
