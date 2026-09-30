-- Indexes, decomposed into their parts, one row per index key.
--
-- Two decisions here prevent two specific kinds of wrong answer.
--
-- 1. The index is NOT compared as pg_get_indexdef() text. That text is re-printed from the
--    catalog and its formatting changes between major releases — NULLS NOT DISTINCT appeared in
--    15, COLLATE printing changed — so two identical indexes on a 15 and a 17 would read as
--    different. The structure is compared instead, and the printed form kept for display only.
--
-- 2. Indexes that merely back a constraint are excluded. A primary key or unique constraint also
--    produces a pg_index row, so without this filter every PK is reported twice: once as the
--    constraint and once as its index. The constraint records its backing index's name instead.
--
-- One row per key rather than one per index: the caller groups them. Still a single query — a
-- round trip per index would be thousands of queries on a real schema.
--
-- ord is zero-based because indkey, indoption, indcollation and indclass are int2vectors, which
-- PostgreSQL subscripts from zero. pg_get_indexdef's column argument is one-based, hence ord + 1.
SELECT
    n.nspname                                        AS schema,
    ic.relname                                       AS name,
    tc.relname                                       AS table_name,
    am.amname                                        AS access_method,
    i.indisunique                                    AS is_unique,
    i.indisprimary                                   AS is_primary,
    i.indisvalid                                     AS is_valid,
    i.indisready                                     AS is_ready,
    i.indnatts                                       AS total_columns,
    i.indnkeyatts                                    AS key_columns,
    {indnullsnotdistinct}                            AS nulls_not_distinct,
    pg_catalog.pg_get_expr(i.indpred, i.indrelid, true) AS predicate,
    ic.reloptions                                    AS reloptions,
    pg_catalog.pg_get_indexdef(i.indexrelid, 0, true) AS definition,
    k.ord                                            AS key_ordinal,
    (k.ord < i.indnkeyatts)                          AS is_key_column,
    -- indkey is 0 where the key is an expression rather than a plain column.
    CASE
        WHEN i.indkey[k.ord] = 0
            THEN pg_catalog.pg_get_indexdef(i.indexrelid, (k.ord + 1)::int, true)
        ELSE ka.attname
    END                                              AS key_expression,
    (i.indkey[k.ord] = 0)                            AS key_is_expression,
    opc.opcname                                      AS opclass,
    opc.opcdefault                                   AS opclass_is_default,
    coll.collname                                    AS key_collation,
    -- indoption bit 0 is DESC, bit 1 is NULLS FIRST. Both change the index's usable orderings.
    (i.indoption[k.ord] & 1) <> 0                    AS descending,
    (i.indoption[k.ord] & 2) <> 0                    AS nulls_first
FROM pg_catalog.pg_index AS i
JOIN pg_catalog.pg_class AS ic ON ic.oid = i.indexrelid
JOIN pg_catalog.pg_class AS tc ON tc.oid = i.indrelid
JOIN pg_catalog.pg_namespace AS n ON n.oid = ic.relnamespace
JOIN pg_catalog.pg_am AS am ON am.oid = ic.relam
CROSS JOIN LATERAL generate_series(0, i.indnatts - 1) AS k(ord)
LEFT JOIN pg_catalog.pg_attribute AS ka
       ON ka.attrelid = i.indrelid AND ka.attnum = i.indkey[k.ord]
LEFT JOIN pg_catalog.pg_opclass AS opc ON opc.oid = i.indclass[k.ord]
LEFT JOIN pg_catalog.pg_collation AS coll
       ON coll.oid = i.indcollation[k.ord] AND coll.collname <> 'default'
WHERE tc.relkind IN ('r', 'p', 'm')
  -- A partitioned index creates a child index on every leaf. Those are inherited, so only the
  -- index on the parent is inventoried.
  AND NOT tc.relispartition
  AND n.nspname = ANY (%(schemas)s::text[])
  -- Excludes the index behind a PK, UNIQUE or EXCLUDE constraint. See the header.
  AND NOT EXISTS (
      SELECT 1
      FROM pg_catalog.pg_constraint AS con
      WHERE con.conindid = i.indexrelid
        AND con.contype IN ('p', 'u', 'x')
  )
  AND NOT EXISTS (
      SELECT 1
      FROM pg_catalog.pg_depend AS dep
      WHERE dep.objid = i.indexrelid
        AND dep.classid = 'pg_catalog.pg_class'::regclass
        AND dep.deptype = 'e'
  )
ORDER BY n.nspname, ic.relname, k.ord
