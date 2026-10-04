"""Version gating.

Each flag guards either a column that does not exist on older servers, or a difference in what
a value means. Both kinds cause silent wrong answers when mis-gated.
"""

import pytest

from db_schema_diff.db.features import (
    MINIMUM_VERSION_LABEL,
    MINIMUM_VERSION_NUM,
    ServerFeatures,
)


def at(version_num):
    return ServerFeatures.from_version_num(version_num)


@pytest.mark.parametrize(
    ("version", "major"), [(110016, 11), (120000, 12), (150004, 15), (170002, 17), (180000, 18)]
)
def test_major_version_is_derived(version, major):
    assert at(version).major == major


class TestSupport:
    def test_the_floor_is_postgresql_11(self):
        """11 has every catalog column the queries need.

        ``pg_index.indnkeyatts`` and ``pg_proc.prokind`` arrived in 11 and ``pg_sequence`` in 10;
        the two columns that arrived in 12 — ``pg_attribute.attgenerated`` and a table's
        ``pg_class.relam`` — are gated and fall back to a literal. The floor is where it is because
        10 lacks ``indnkeyatts``, not because 11 is untested.
        """
        assert MINIMUM_VERSION_NUM == 110000
        assert at(110000).supported
        assert at(110016).supported

    def test_the_label_and_the_number_say_the_same_thing(self):
        """Two constants that have to agree, and the refusal message quotes the label.

        Changing one and not the other produces "PostgreSQL 11 is too old; this tool needs 11 or
        newer" — a sentence that sends somebody to upgrade a server they already have.
        """
        assert str(MINIMUM_VERSION_NUM // 10000) == MINIMUM_VERSION_LABEL

    def test_postgresql_10_is_not_supported(self):
        assert not at(100000).supported
        assert not at(100023).supported

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

    def test_the_two_columns_that_arrived_in_12_fall_back_on_11(self):
        """The whole reason 11 can be inspected at all: neither column is selected by name."""
        eleven = at(110016).placeholders()
        assert eleven["attgenerated"] == "''::\"char\""
        assert eleven["relam"] == "NULL::name"
        twelve = at(120000).placeholders()
        assert twelve["attgenerated"] == "a.attgenerated"
        assert twelve["relam"] == "am.amname"

    def test_every_placeholder_is_present_on_every_supported_version(self):
        expected = set(at(170000).placeholders())
        for version in (110000, 120000, 130000, 140000, 150000, 160000, 170000):
            assert set(at(version).placeholders()) == expected

    def test_placeholders_contain_no_query_parameters(self):
        # These are interpolated into SQL, so they must be fixed text chosen by version only.
        for value in at(150004).placeholders().values():
            assert "%" not in value
            assert ";" not in value
