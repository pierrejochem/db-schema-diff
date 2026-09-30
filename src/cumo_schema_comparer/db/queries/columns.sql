-- Columns of every table in the selected schemas.
--
-- Tables only. A view's columns are carried by the view object as `name type` pairs, because a
-- view's column list is derived from its query: inventorying them separately reports every view
-- change twice, once as the view and once per column.
--
-- Two details here are the difference between a working comparer and a noisy one.
--
-- 1. The dense ordinal. attnum has permanent holes wherever a column was ever dropped, so
--    comparing it reports drift on every column after the hole in any database that has had a
--    DROP COLUMN. row_number() over the live columns is the position a person would count.
--
-- 2. format_type(atttypid, atttypmod) is the single source of truth for the type. Rebuilding
--    it from information_schema's data_type plus character_maximum_length, numeric_precision
--    and numeric_scale is the classic way to get this wrong, because those columns disagree
--    about what "unspecified" means.
--
-- owned_sequence comes from pg_depend with deptype='a': the sequence a serial column owns. Its
-- generated name diverges between environments, so the default is canonicalised to a sentinel
-- and the name reported for information only.
SELECT
    n.nspname                                                  AS schema,
    c.relname                                                  AS name,
    a.attname                                                  AS subname,
    row_number() OVER (
        PARTITION BY a.attrelid ORDER BY a.attnum
    )                                                          AS ordinal,
    a.attnum                                                   AS attnum,
    pg_catalog.format_type(a.atttypid, a.atttypmod)             AS data_type,
    NOT a.attnotnull                                           AS is_nullable,
    pg_catalog.pg_get_expr(ad.adbin, ad.adrelid, true)         AS default_expr,
    NULLIF(a.attidentity, '')                                  AS identity,
    NULLIF({attgenerated}, '')                                 AS generated_kind,
    CASE WHEN {attgenerated} = 's'
         THEN pg_catalog.pg_get_expr(ad.adbin, ad.adrelid, true) END AS generated_expr,
    coll.collname                                              AS collation,
    seq.relname                                                AS owned_sequence,
    c.relkind                                                  AS relkind
FROM pg_catalog.pg_attribute AS a
JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
LEFT JOIN pg_catalog.pg_attrdef AS ad
       ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
LEFT JOIN pg_catalog.pg_collation AS coll ON coll.oid = a.attcollation
                                         AND coll.collname <> 'default'
-- At most one row, guaranteed by the LATERAL. This must not be written as a plain join to
-- pg_depend with pg_class filtered afterwards: an *index* also depends on its table's columns with
-- deptype 'a', so the join would emit one row per dependent index. The duplicates then inflate the
-- row_number() above, and the ordinal of a column silently depends on how many indexes mention it.
LEFT JOIN LATERAL (
    SELECT s.relname
    FROM pg_catalog.pg_depend AS d
    JOIN pg_catalog.pg_class AS s ON s.oid = d.objid AND s.relkind = 'S'
    WHERE d.refobjid = a.attrelid
      AND d.refobjsubid = a.attnum
      AND d.classid = 'pg_catalog.pg_class'::regclass
      AND d.deptype = 'a'
    LIMIT 1
) AS seq ON true
WHERE a.attnum > 0
  AND NOT a.attisdropped
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
      WHERE dep.objid = c.oid
        AND dep.classid = 'pg_catalog.pg_class'::regclass
        AND dep.deptype = 'e'
  )
ORDER BY n.nspname, c.relname, a.attnum
