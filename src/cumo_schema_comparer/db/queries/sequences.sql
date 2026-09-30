-- Sequences, by definition rather than by position.
--
-- last_value and is_called are absent on purpose. They are runtime state: a production sequence has
-- issued more values than a QA one by definition, so comparing them reports every sequence in the
-- database as drifted. The *definition* — start, increment, bounds, cache, cycle — is what a
-- migration sets and what can actually differ by mistake.
SELECT
    n.nspname                                  AS schema,
    c.relname                                  AS name,
    pg_catalog.format_type(s.seqtypid, NULL)   AS data_type,
    s.seqstart                                 AS start_value,
    s.seqincrement                             AS increment,
    s.seqmin                                   AS min_value,
    s.seqmax                                   AS max_value,
    s.seqcache                                 AS cache_size,
    s.seqcycle                                 AS cycles,
    owner.owned_by                             AS owned_by
FROM pg_catalog.pg_sequence AS s
JOIN pg_catalog.pg_class AS c ON c.oid = s.seqrelid
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
-- The column this sequence belongs to, if it is a serial's. At most one row.
LEFT JOIN LATERAL (
    SELECT t.relname || '.' || a.attname AS owned_by
    FROM pg_catalog.pg_depend AS d
    JOIN pg_catalog.pg_class AS t ON t.oid = d.refobjid
    JOIN pg_catalog.pg_attribute AS a
      ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
    WHERE d.objid = s.seqrelid
      AND d.classid = 'pg_catalog.pg_class'::regclass
      AND d.deptype IN ('a', 'i')
      AND d.refobjsubid > 0
    LIMIT 1
) AS owner ON true
WHERE n.nspname = ANY (%(schemas)s::text[])
  AND NOT EXISTS (
      SELECT 1
      FROM pg_catalog.pg_depend AS dep
      WHERE dep.objid = c.oid
        AND dep.classid = 'pg_catalog.pg_class'::regclass
        AND dep.deptype = 'e'
  )
ORDER BY n.nspname, c.relname
