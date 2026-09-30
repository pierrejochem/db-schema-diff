-- Installed extensions.
--
-- Compared as a unit. An extension installs hundreds or thousands of functions, types and operators;
-- inventorying them individually would let postgis dominate every report, so they are filtered out
-- everywhere else and this one row stands in for all of them. A version difference is what actually
-- matters, and it is exactly what the individual objects would be evidence of.
SELECT
    n.nspname     AS schema,
    e.extname     AS name,
    e.extversion  AS version
FROM pg_catalog.pg_extension AS e
JOIN pg_catalog.pg_namespace AS n ON n.oid = e.extnamespace
ORDER BY e.extname
