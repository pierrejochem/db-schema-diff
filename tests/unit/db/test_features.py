"""Version gating.

Each flag guards either a column that does not exist on older servers, or a difference in what
a value means. Both kinds cause silent wrong answers when mis-gated.
"""

import pytest

from cumo_schema_comparer.db.features import MINIMUM_VERSION_NUM, ServerFeatures


def at(version_num):
    return ServerFeatures.from_version_num(version_num)


@pytest.mark.parametrize(
    ("version", "major"), [(120000, 12), (150004, 15), (170002, 17), (180000, 18)]
)
def test_major_version_is_derived(version, major):
    assert at(version).major == major


class TestSupport:
    def test_the_floor_is_postgresql_12(self):
        assert MINIMUM_VERSION_NUM == 120000
        assert at(120000).supported
        assert not at(110012).supported

    def test_a_newer_release_is_supported(self):
        assert at(180000).supported


class TestFlags:
    @pytest.mark.parametrize(
        ("flag", "introduced"),
        [
            ("generated_columns", 120000),
            ("table_access_methods", 120000),
            ("trigger_parent", 130000),
            ("sql_standard_bodies", 140000),
            ("attribute_compression", 140000),
            ("nulls_not_distinct", 150000),
            ("not_null_constraints", 170000),
        ],
    )
    def test_each_flag_turns_on_at_its_release(self, flag, introduced):
        assert getattr(at(introduced), flag) is True
        assert getattr(at(introduced - 10000), flag) is False


class TestPlaceholders:
    def test_a_missing_column_is_replaced_by_a_literal(self):
        # Selecting a column that does not exist fails the whole query, so older servers must
        # get a literal of the same shape instead.
        old = at(140000).placeholders()
        assert old["indnullsnotdistinct"] == "false"
        new = at(150000).placeholders()
        assert new["indnullsnotdistinct"] == "i.indnullsnotdistinct"

    def test_every_placeholder_is_present_on_every_supported_version(self):
        expected = set(at(170000).placeholders())
        for version in (120000, 130000, 140000, 150000, 160000, 170000):
            assert set(at(version).placeholders()) == expected

    def test_placeholders_contain_no_query_parameters(self):
        # These are interpolated into SQL, so they must be fixed text chosen by version only.
        for value in at(150004).placeholders().values():
            assert "%" not in value
            assert ";" not in value
