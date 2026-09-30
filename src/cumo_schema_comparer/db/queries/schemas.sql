-- Schemas to inventory.
--
-- The NOT LIKE 'pg\_%%' test covers pg_catalog, pg_toast, every pg_temp_N and pg_toast_temp_N
-- in one condition, which matters because temporary schemas are numbered per backend and
-- cannot be listed exhaustively.
--
-- %(exclude)s is the user's --exclude-schema list and %(only)s an explicit allowlist. Both are
-- query parameters, never interpolated.
--
-- The doubled percent in 'pg\_%%' is required: psycopg parses placeholders in every query that
-- carries a parameter mapping, so a lone %% would be read as a malformed placeholder.
SELECT n.nspname AS schema
FROM pg_catalog.pg_namespace AS n
WHERE n.nspname NOT LIKE 'pg\_%%'
  AND n.nspname <> 'information_schema'
  AND (%(only)s::text[] IS NULL OR n.nspname = ANY (%(only)s::text[]))
  AND NOT (n.nspname = ANY (%(exclude)s::text[]))
  -- Schemas owned by an extension belong to that extension; its version is compared instead.
  AND NOT EXISTS (
      SELECT 1
      FROM pg_catalog.pg_depend AS dep
      WHERE dep.objid = n.oid
        AND dep.classid = 'pg_catalog.pg_namespace'::regclass
        AND dep.deptype = 'e'
  )
ORDER BY n.nspname
