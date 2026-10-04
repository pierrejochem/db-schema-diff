-- User triggers.
--
-- NOT tgisinternal is mandatory, not an optimisation. PostgreSQL creates an internal trigger pair
-- for every foreign key constraint, so a 200-table schema has thousands of them and every one would
-- appear in the report.
--
-- tgenabled is compared: a trigger disabled in one environment is the classic production-only
-- workaround, and it is invisible to anything that only checks whether the trigger exists.
--
-- Both the WHEN clause and the arguments are read out of pg_get_triggerdef rather than from the
-- catalog columns directly.
--
-- For the arguments that is a convenience: tgargs is a null-separated bytea whose encoding is fiddly
-- and version-sensitive for no benefit.
--
-- For the WHEN clause it is a necessity. pg_get_expr(tgqual, tgrelid) *fails* — "expression contains
-- variables of more than one relation" — because a trigger condition references both NEW and OLD, and
-- pg_get_expr can only be told about one relation. Verified against PostgreSQL 15.
SELECT
    n.nspname                                          AS schema,
    c.relname                                          AS name,
    t.tgname                                           AS subname,
    t.tgtype                                           AS tgtype,
    t.tgenabled                                        AS enabled,
    t.tgdeferrable                                     AS deferrable,
    t.tginitdeferred                                   AS deferred,
    fn.nspname || '.' || p.proname                     AS function_name,
    (t.tgqual IS NOT NULL)                             AS has_condition,
    pg_catalog.pg_get_triggerdef(t.oid, true)          AS definition,
    {tgparentid}                                       AS parent_id
FROM pg_catalog.pg_trigger AS t
JOIN pg_catalog.pg_class AS c ON c.oid = t.tgrelid
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_proc AS p ON p.oid = t.tgfoid
JOIN pg_catalog.pg_namespace AS fn ON fn.oid = p.pronamespace
WHERE NOT t.tgisinternal
  AND n.nspname = ANY (%(schemas)s::text[])
ORDER BY n.nspname, c.relname, t.tgname
