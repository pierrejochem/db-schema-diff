-- Tables, partitioned parents and partition leaves.
--
-- relpersistence is compared because an unlogged table in one environment and a permanent one
-- in another silently changes crash-recovery behaviour. Row estimates (relpages, reltuples)
-- are deliberately absent: they are runtime state and differ between environments by design.
SELECT
    n.nspname                                          AS schema,
    c.relname                                          AS name,
    c.relkind                                          AS relkind,
    c.relpersistence                                   AS persistence,
    c.relispartition                                   AS is_partition,
    (c.relkind = 'p')                                  AS is_partitioned,
    CASE WHEN c.relkind = 'p'
         THEN pg_catalog.pg_get_partkeydef(c.oid) END  AS partition_key,
    CASE WHEN c.relispartition
         THEN pg_catalog.pg_get_expr(c.relpartbound, c.oid, true) END AS partition_bound,
    c.reloptions                                       AS reloptions,
    -- The parent of a leaf partition. Leaves are folded into a count on their parent rather than
    -- inventoried individually: they are created by a scheduled maintenance job, not by a migration,
    -- so production legitimately holds far more of them than a development database.
    parent.relname                                     AS partition_parent,
    {relam}                                            AS access_method
FROM pg_catalog.pg_class AS c
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
LEFT JOIN pg_catalog.pg_am AS am ON am.oid = c.relam
LEFT JOIN pg_catalog.pg_inherits AS inh ON inh.inhrelid = c.oid AND c.relispartition
LEFT JOIN pg_catalog.pg_class AS parent ON parent.oid = inh.inhparent
WHERE c.relkind IN ('r', 'p')
  AND n.nspname = ANY (%(schemas)s::text[])
  -- An extension's own tables belong to the extension, whose version is compared instead.
  AND NOT EXISTS (
      SELECT 1
      FROM pg_catalog.pg_depend AS dep
      WHERE dep.objid = c.oid
        AND dep.classid = 'pg_catalog.pg_class'::regclass
        AND dep.deptype = 'e'
  )
ORDER BY n.nspname, c.relname
