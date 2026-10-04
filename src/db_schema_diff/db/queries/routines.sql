-- Functions and procedures.
--
-- The body comes from prosrc, NOT from pg_get_functiondef(). That function re-prints the whole
-- CREATE statement, and its header formatting changes between major releases, so comparing it
-- reports every routine in the database as changed when a 15 is compared against a 17.
--
-- Identity includes pg_get_function_identity_arguments(), because overloads are distinct objects
-- that share a name. Without it, two overloads collapse into one and each reports the other's body
-- as drift.
--
-- proconfig is compared: a routine with `SET search_path` in one environment and not in another is a
-- real behavioural and security difference, not a detail.
--
-- Two kinds of routine are excluded because they are not authored, they are generated.
--
-- Extension-owned ones (deptype 'e'): postgis installs thousands, and every one would be reported.
-- The extension's version is compared instead.
--
-- Internally-dependent ones (deptype 'i'): `CREATE TYPE ... AS RANGE` generates a constructor
-- function per signature, and those exist purely because the type exists. Verified: a single range
-- type contributed five such functions to the inventory. The type itself is compared, so a
-- difference in it is already reported once.
SELECT
    n.nspname                                                  AS schema,
    p.proname                                                  AS name,
    pg_catalog.pg_get_function_identity_arguments(p.oid)        AS identity_arguments,
    pg_catalog.pg_get_function_arguments(p.oid)                 AS arguments,
    p.prokind                                                  AS prokind,
    l.lanname                                                  AS language,
    pg_catalog.format_type(p.prorettype, NULL)                 AS return_type,
    p.proretset                                                AS returns_set,
    p.provolatile                                              AS volatility,
    p.proisstrict                                              AS strict,
    p.prosecdef                                                AS security_definer,
    p.proparallel                                              AS parallel_safety,
    p.proconfig                                                AS config,
    pg_catalog.pg_get_expr(p.proargdefaults, 0)                AS argument_defaults,
    p.prosrc                                                   AS body,
    {prosqlbody}                                               AS sql_body
FROM pg_catalog.pg_proc AS p
JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
JOIN pg_catalog.pg_language AS l ON l.oid = p.prolang
WHERE n.nspname = ANY (%(schemas)s::text[])
  AND NOT EXISTS (
      SELECT 1
      FROM pg_catalog.pg_depend AS dep
      WHERE dep.objid = p.oid
        AND dep.classid = 'pg_catalog.pg_proc'::regclass
        AND dep.deptype IN ('e', 'i')
  )
ORDER BY n.nspname, p.proname
