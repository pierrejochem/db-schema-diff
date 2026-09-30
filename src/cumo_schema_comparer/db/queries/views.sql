-- Views and materialized views.
--
-- pg_get_viewdef() re-prints the query from the parse tree, so the text is already normalised by the
-- server — but the *printing* changes between major releases, which is why the body is compared at
-- warning level and the resolved column list at error level. A consumer breaks when a column
-- disappears regardless of how the query behind it was rewritten.
--
-- The column list carries each column's type as well as its name, and is the *only* place a view's
-- columns are inventoried: the columns query covers tables alone. Capturing both would report every
-- view change twice, once as the view and once per column.
--
-- ispopulated is deliberately absent: whether a matview has been refreshed is runtime state.
SELECT
    n.nspname                                          AS schema,
    c.relname                                          AS name,
    (c.relkind = 'm')                                  AS is_materialized,
    pg_catalog.pg_get_viewdef(c.oid, true)             AS definition,
    c.reloptions                                       AS reloptions,
    cols.names                                         AS columns
FROM pg_catalog.pg_class AS c
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
LEFT JOIN LATERAL (
    SELECT array_agg(
               a.attname || ' ' || pg_catalog.format_type(a.atttypid, a.atttypmod)
               ORDER BY a.attnum
           ) AS names
    FROM pg_catalog.pg_attribute AS a
    WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
) AS cols ON true
WHERE c.relkind IN ('v', 'm')
  AND n.nspname = ANY (%(schemas)s::text[])
  AND NOT EXISTS (
      SELECT 1
      FROM pg_catalog.pg_depend AS dep
      WHERE dep.objid = c.oid
        AND dep.classid = 'pg_catalog.pg_class'::regclass
        AND dep.deptype = 'e'
  )
ORDER BY n.nspname, c.relname
