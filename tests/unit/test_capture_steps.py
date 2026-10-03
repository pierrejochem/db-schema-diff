"""The steps a capture announces as it runs.

The desktop application lays out a checklist before a comparison starts and fills it in as each
step finishes, so these two have to agree about what a capture does and in what order. The failure
this file exists to prevent is quiet: a new object kind captured outside the step table would be in
the report and absent from the one place that claims to say what was looked at, and nothing else
would complain.
"""

from __future__ import annotations

import inspect

from cumo_schema_comparer import build
from cumo_schema_comparer.build import (
    _CAPTURE_STEPS,
    _STEP_SUBKINDS,
    CAPTURE_ROWS,
    CAPTURE_STEP_NAMES,
    CHANGELOG_STEP,
    SCHEMA_STEP,
    STEP_KINDS,
)
from cumo_schema_comparer.model.kinds import ObjectKind


class TestTheDeclaredStepsMatchTheTable:
    def test_the_names_are_the_table_in_order(self):
        """The checklist is laid out from the names and filled in from the table."""
        assert tuple(name for name, _, _ in _CAPTURE_STEPS) == CAPTURE_STEP_NAMES

    def test_no_step_is_declared_twice(self):
        assert len(set(CAPTURE_STEP_NAMES)) == len(CAPTURE_STEP_NAMES)

    def test_the_changelog_is_not_one_of_them(self):
        """It is read after the catalog and only when it is not skipped, so it is its own step."""
        assert CHANGELOG_STEP not in CAPTURE_STEP_NAMES


class TestTheRowsACaptureReports:
    """What a caller lays the checklist out from, which has to match what arrives."""

    def test_the_schema_row_comes_first(self):
        """It is the one number that explains all the others: a filter that matched nothing makes
        every count below it zero, and the zeros give no reason for themselves."""
        assert CAPTURE_ROWS[0] == SCHEMA_STEP

    def test_every_step_is_a_row(self):
        assert set(CAPTURE_STEP_NAMES) <= set(CAPTURE_ROWS)

    def test_a_sub_row_follows_the_step_it_breaks_down(self):
        for step, subs in STEP_KINDS.items():
            at = CAPTURE_ROWS.index(step)
            assert list(CAPTURE_ROWS[at + 1 : at + 1 + len(subs)]) == list(subs)

    def test_no_row_is_named_twice(self):
        """Rows are addressed by name, so a duplicate would have two rows fighting over one."""
        assert len(set(CAPTURE_ROWS)) == len(CAPTURE_ROWS)

    def test_the_sub_row_names_and_their_kinds_stay_in_step(self):
        assert set(STEP_KINDS) == set(_STEP_SUBKINDS)
        for step, subs in STEP_KINDS.items():
            assert len(subs) == len(_STEP_SUBKINDS[step]), step

    def test_materialized_views_are_counted_separately_from_views(self):
        """One query returns both, so a single "views: 3" cannot answer how many matviews exist —
        and a matview holds data and must be refreshed, so it is a different thing."""
        assert "materialized views" in STEP_KINDS["views"]
        assert ObjectKind.MATVIEW in _STEP_SUBKINDS["views"]

    def test_every_kind_a_multi_kind_step_yields_has_a_row(self):
        """Otherwise a kind is inside a total and nameable nowhere."""
        from cumo_schema_comparer.build import _CAPTURE_STEPS as steps

        multi = {name for name in STEP_KINDS}
        assert multi <= {name for name, _, _ in steps}


class TestEveryKindIsCoveredByAStep:
    """A kind captured outside the table would never be announced."""

    def test_the_table_accounts_for_every_object_kind(self):
        # Several kinds share one step: a step is one catalog query, and `types` yields enums,
        # domains, composites and ranges together. So this maps kinds onto steps rather than
        # expecting one each.
        covered = {
            ObjectKind.TABLE: "tables",
            ObjectKind.COLUMN: "columns",
            ObjectKind.CONSTRAINT: "constraints",
            ObjectKind.INDEX: "indexes",
            ObjectKind.VIEW: "views",
            ObjectKind.MATVIEW: "views",
            ObjectKind.SEQUENCE: "sequences",
            ObjectKind.ROUTINE: "routines",
            ObjectKind.TRIGGER: "triggers",
            ObjectKind.ENUM_TYPE: "types",
            ObjectKind.DOMAIN_TYPE: "types",
            ObjectKind.COMPOSITE_TYPE: "types",
            ObjectKind.RANGE_TYPE: "types",
            ObjectKind.EXTENSION: "extensions",
            # Schemas are the scope every other query is given, not a captured object.
            ObjectKind.SCHEMA: None,
        }
        assert set(covered) == set(ObjectKind), "a new object kind needs a step to announce it"
        assert {step for step in covered.values() if step} == set(CAPTURE_STEP_NAMES)


class TestNothingIsCapturedOutsideTheTable:
    def test_build_inventory_fills_objects_only_from_the_loop(self):
        """Reads the source: an ``objects.update`` added beside the loop would be unannounced.

        A test about the shape of the code, which is unusual, and the only way to catch the thing
        that actually goes wrong here — adding a kind the way the previous ten were written.
        """
        source = inspect.getsource(build.build_inventory)
        updates = [line.strip() for line in source.splitlines() if "objects.update(" in line]
        assert updates == ["objects.update(found)"], (
            "every captured kind must come through _CAPTURE_STEPS so that it is announced"
        )
