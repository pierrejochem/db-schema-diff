"""The capture steps a real database reports.

The desktop application shows these as a checklist while a comparison runs, which is a promise
about what was looked at. A stub can prove the wiring carries a number; only a real capture proves
the number is the one the catalog returned, and that every declared step actually fires.

The failure this catches is the quiet kind: a step renamed or reordered in ``build`` while the
checklist keeps laying out the old names, so a row sits at "pending" for every run and nobody can
tell it apart from a capture that stalled.
"""

from __future__ import annotations

import pytest

from cumo_schema_comparer.build import CAPTURE_STEP_NAMES, CHANGELOG_STEP, build_inventory
from cumo_schema_comparer.config.model import SourceRef
from cumo_schema_comparer.config.secrets import Dsn
from cumo_schema_comparer.db.connect import open_connection
from cumo_schema_comparer.model.kinds import ObjectKind

pytestmark = pytest.mark.integration

SOURCE = SourceRef(label="prod", dsn_env="OBS_DSN")


def capture(dsn: str, **kwargs):
    """Capture with an observer attached, returning the inventory and what it reported."""
    seen: list[tuple[str, int]] = []

    def observe(step: str, count: int) -> None:
        seen.append((step, count))

    with open_connection(Dsn(dsn, env_name=SOURCE.dsn_env), label=SOURCE.label) as connection:
        inventory = build_inventory(connection, SOURCE, observer=observe, **kwargs)
    return inventory, seen


@pytest.fixture(scope="module")
def observed(read_only_master: str):
    return capture(read_only_master)


class TestEveryStepReports:
    def test_every_declared_step_fires_exactly_once(self, observed):
        _, seen = observed
        catalog = [name for name, _ in seen if name != CHANGELOG_STEP]
        assert catalog == list(CAPTURE_STEP_NAMES)

    def test_they_arrive_in_the_declared_order(self, observed):
        """The checklist is laid out from the names before anything runs."""
        _, seen = observed
        assert [name for name, _ in seen][: len(CAPTURE_STEP_NAMES)] == list(CAPTURE_STEP_NAMES)

    def test_the_changelog_is_reported_after_the_catalog(self, observed):
        _, seen = observed
        assert seen[-1][0] == CHANGELOG_STEP


class TestTheCountsAreReal:
    """A stub can report any number. These are the catalog's own."""

    def test_each_count_matches_what_the_inventory_holds(self, observed):
        inventory, seen = observed
        counts = dict(seen)
        by_kind = inventory.counts()
        assert counts["tables"] == by_kind[ObjectKind.TABLE]
        assert counts["columns"] == by_kind[ObjectKind.COLUMN]
        assert counts["indexes"] == by_kind[ObjectKind.INDEX]
        assert counts["routines"] == by_kind[ObjectKind.ROUTINE]
        # One step yields four kinds, because a step is one catalog query.
        assert counts["types"] == sum(
            by_kind.get(kind, 0)
            for kind in (
                ObjectKind.ENUM_TYPE,
                ObjectKind.DOMAIN_TYPE,
                ObjectKind.COMPOSITE_TYPE,
                ObjectKind.RANGE_TYPE,
            )
        )

    def test_the_fixture_is_rich_enough_for_this_to_mean_something(self, observed):
        """Every count being zero would satisfy the assertions above and prove nothing."""
        _, seen = observed
        counts = dict(seen)
        for step in ("tables", "columns", "indexes", "routines", "views", "types"):
            assert counts[step] > 0, f"{step} found nothing, so its count proves nothing"


class TestSkippingTheChangelog:
    def test_no_changelog_step_is_reported_when_it_is_skipped(self, read_only_master):
        """The checklist omits the row in that case, so reporting it would never be shown."""
        _, seen = capture(read_only_master, skip_liquibase=True)
        assert CHANGELOG_STEP not in [name for name, _ in seen]
        assert [name for name, _ in seen] == list(CAPTURE_STEP_NAMES)


class TestAnObserverIsOptional:
    def test_a_capture_without_one_still_works(self, read_only_master):
        with open_connection(
            Dsn(read_only_master, env_name=SOURCE.dsn_env), label=SOURCE.label
        ) as connection:
            assert len(build_inventory(connection, SOURCE)) > 0

    def test_an_observer_that_raises_does_not_fail_the_capture(self, read_only_master):
        """A broken progress display is not a reason to lose a capture that already succeeded."""

        def explode(step: str, count: int) -> None:
            raise RuntimeError("the dialog is broken")

        with open_connection(
            Dsn(read_only_master, env_name=SOURCE.dsn_env), label=SOURCE.label
        ) as connection:
            assert len(build_inventory(connection, SOURCE, observer=explode)) > 0
