-- Primary keys, unique, foreign key, check and exclusion constraints.
--
-- Structural identity comes from the resolved column *names in order*, never from conkey's
-- attribute numbers: those differ between two databases whose columns were added in a different
-- order, while the constraint is the same constraint.
--
-- contype 'n' is excluded deliberately. PostgreSQL 17 began exposing NOT NULL as a pg_constraint
-- row of that type; including it would invent one constraint per column when a 15 is compared
-- against a 17. Nullability is read from pg_attribute.attnotnull on every version instead.
--
-- pg_get_constraintdef() is captured for display. The comparison uses the decomposed columns,
-- because the printed form changes between major releases.
SELECT
    n.nspname                                          AS schema,
    c.relname                                          AS name,
    con.conname                                        AS subname,
    con.contype                                        AS contype,
    con.condeferrable                                  AS deferrable,
    con.condeferred                                    AS deferred,
    con.convalidated                                   AS validated,
    con.confupdtype                                    AS on_update,
    con.confdeltype                                    AS on_delete,
    con.confmatchtype                                  AS match_type,
    pg_catalog.pg_get_constraintdef(con.oid, true)     AS definition,
    -- The referenced table, for a foreign key. Qualified, so a target that moved a table between
    -- schemas is reported rather than silently accepted.
    fn.nspname                                         AS references_schema,
    fc.relname                                         AS references_name,
    cols.names                                         AS columns,
    refcols.names                                      AS references_columns,
    con.conindid <> 0                                  AS has_backing_index,
    bi.relname                                         AS backing_index
FROM pg_catalog.pg_constraint AS con
JOIN pg_catalog.pg_class AS c ON c.oid = con.conrelid
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
LEFT JOIN pg_catalog.pg_class AS fc ON fc.oid = con.confrelid
LEFT JOIN pg_catalog.pg_namespace AS fn ON fn.oid = fc.relnamespace
LEFT JOIN pg_catalog.pg_class AS bi ON bi.oid = con.conindid
-- Column names in the constraint's own order. ordinality preserves it; array_agg alone would not.
LEFT JOIN LATERAL (
    SELECT array_agg(a.attname ORDER BY k.ord) AS names
    FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
    JOIN pg_catalog.pg_attribute AS a
      ON a.attrelid = con.conrelid AND a.attnum = k.attnum
) AS cols ON true
LEFT JOIN LATERAL (
    SELECT array_agg(a.attname ORDER BY k.ord) AS names
    FROM unnest(con.confkey) WITH ORDINALITY AS k(attnum, ord)
    JOIN pg_catalog.pg_attribute AS a
      ON a.attrelid = con.confrelid AND a.attnum = k.attnum
) AS refcols ON true
WHERE con.contype IN ('p', 'u', 'f', 'c', 'x')
  AND c.relkind IN ('r', 'p')
  -- A leaf partition's structure is inherited from its parent and cannot differ, so it is not
  -- inventoried: the parent is compared and the leaves are counted. Without this, a production
  -- database with sixty monthly partitions reports hundreds of findings against a development
  -- one with two.
  AND NOT c.relispartition
  AND n.nspname = ANY (%(schemas)s::text[])
  AND NOT EXISTS (
      SELECT 1
      FROM pg_catalog.pg_depend AS dep
      WHERE dep.objid = con.oid
        AND dep.classid = 'pg_catalog.pg_constraint'::regclass
        AND dep.deptype = 'e'
  )
ORDER BY n.nspname, c.relname, con.conname
