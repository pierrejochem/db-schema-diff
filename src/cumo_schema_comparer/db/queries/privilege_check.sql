-- Objects whose definition came back NULL, per kind.
--
-- pg_get_viewdef() and prosrc are NULL for an object the connecting role cannot see. Compared as
-- data that reads as "definition differs" -- drift that is really a permissions problem. The
-- connection check counts them instead of capturing a whole inventory, so it stays fast.
--
-- Extension-owned and internally-dependent routines (deptype 'e', 'i') are skipped for the same
-- reason routines.sql skips them: they are generated, not authored.
SELECT kind, count
FROM (
    SELECT
        CASE c.relkind WHEN 'v' THEN 'view' ELSE 'matview' END AS kind,
        count(*)                                               AS count
    FROM pg_catalog.pg_class AS c
    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
    WHERE n.nspname = ANY (%(schemas)s::text[])
      AND c.relkind IN ('v', 'm')
      AND pg_catalog.pg_get_viewdef(c.oid, true) IS NULL
    GROUP BY c.relkind
    UNION ALL
    SELECT 'routine' AS kind, count(*) AS count
    FROM pg_catalog.pg_proc AS p
    JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
    WHERE n.nspname = ANY (%(schemas)s::text[])
      AND p.prokind IN ('f', 'p')
      AND p.prosrc IS NULL
      AND NOT EXISTS (
          SELECT 1
          FROM pg_catalog.pg_depend AS dep
          WHERE dep.objid = p.oid
            AND dep.classid = 'pg_catalog.pg_proc'::regclass
            AND dep.deptype IN ('e', 'i')
      )
) AS gaps
WHERE count > 0
ORDER BY kind
