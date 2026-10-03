"""Version-dependent catalog capabilities.

The supported floor is PostgreSQL 11, because ``pg_index.indnkeyatts`` and ``pg_proc.prokind``
arrived in 11 and are selected by name on every version. ``pg_sequence`` (10) is older than that,
and the two columns that arrived in 12 — ``pg_attribute.attgenerated`` and a table's
``pg_class.relam`` — are gated below, so neither is ever named in a query sent to an 11. The floor
is therefore where 10 stops being inspectable, not where this tool stops being tested: 11 has its
own integration coverage in ``tests/integration/test_pg11.py``.

Most of this platform is 15 or newer. The floor exists so a server older than 11 fails with a
clear message rather than a confusing SQL error.

Feature flags are used for two different purposes, and mixing them up causes silent bugs:

* *selecting* a column that does not exist fails the whole query, so the flag chooses between
  a real column and a literal fallback;
* a feature can also change what a value *means*. ``not_null_constraints`` is the sharp one:
  PostgreSQL 17 exposes NOT NULL as a ``pg_constraint`` row of type ``'n'``, so comparing a 15
  against a 17 invents one constraint per column unless those rows are filtered out.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..model.inventory import GENERATED_COLUMNS_FROM

#: Oldest release this tool will inspect.
MINIMUM_VERSION_NUM = 110000
MINIMUM_VERSION_LABEL = "11"


@dataclass(frozen=True, slots=True)
class ServerFeatures:
    """What one server's catalog offers."""

    version_num: int

    @classmethod
    def from_version_num(cls, version_num: int) -> ServerFeatures:
        return cls(version_num=version_num)

    @property
    def major(self) -> int:
        """Major release number, e.g. ``15``."""
        return self.version_num // 10000

    @property
    def supported(self) -> bool:
        return self.version_num >= MINIMUM_VERSION_NUM

    @property
    def generated_columns(self) -> bool:
        """``pg_attribute.attgenerated`` — ``GENERATED ALWAYS AS (...) STORED``. PG 12.

        An 11 reports every column as not generated. That is a fact about its catalog and not
        about its schema, so a comparison across the boundary stops comparing the attribute
        rather than reporting one difference per generated column — see
        ``diff.engine._adjust_for_sources``.
        """
        return self.version_num >= GENERATED_COLUMNS_FROM

    @property
    def table_access_methods(self) -> bool:
        """``pg_class.relam`` is meaningful for tables. PG 12."""
        return self.version_num >= 120000

    @property
    def trigger_parent(self) -> bool:
        """``pg_trigger.tgparentid`` — partition-inherited triggers. PG 13."""
        return self.version_num >= 130000

    @property
    def sql_standard_bodies(self) -> bool:
        """``pg_proc.prosqlbody`` — standard-body SQL functions leave ``prosrc`` empty. PG 14."""
        return self.version_num >= 140000

    @property
    def attribute_compression(self) -> bool:
        """``pg_attribute.attcompression``. PG 14. Captured but never compared."""
        return self.version_num >= 140000

    @property
    def nulls_not_distinct(self) -> bool:
        """``pg_index.indnullsnotdistinct`` — ``UNIQUE NULLS NOT DISTINCT``. PG 15."""
        return self.version_num >= 150000

    @property
    def not_null_constraints(self) -> bool:
        """``pg_constraint`` rows of type ``'n'`` for NOT NULL. PG 17.

        Always filtered out. Nullability comes from ``pg_attribute.attnotnull`` on every
        version, so that a 15-against-17 comparison does not report one phantom constraint per
        column.
        """
        return self.version_num >= 170000

    def placeholders(self) -> dict[str, str]:
        """SQL fragments the query files interpolate.

        These carry no user data — only fixed column names and literals chosen by version — so
        interpolating them is safe. Every value that comes from a user or a catalog is passed
        as a query parameter instead.
        """
        return {
            "attgenerated": "a.attgenerated" if self.generated_columns else "''::\"char\"",
            "indnullsnotdistinct": (
                "i.indnullsnotdistinct" if self.nulls_not_distinct else "false"
            ),
            "prosqlbody": (
                "pg_get_function_sqlbody(p.oid)" if self.sql_standard_bodies else "NULL::text"
            ),
            "tgparentid": "t.tgparentid" if self.trigger_parent else "0::oid",
            "relam": "am.amname" if self.table_access_methods else "NULL::name",
        }
