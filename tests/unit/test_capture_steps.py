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
from cumo_schema_comparer.build import _CAPTURE_STEPS, CAPTURE_STEP_NAMES, CHANGELOG_STEP
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
