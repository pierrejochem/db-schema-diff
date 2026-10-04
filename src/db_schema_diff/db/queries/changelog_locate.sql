-- Every Liquibase bookkeeping table in the database.
--
-- Located rather than assumed, for a verified reason: acme-invoicing sets
-- spring.liquibase.liquibase-schema=acme-invoicing, so its DATABASECHANGELOG lives in a
-- quoted, hyphenated schema and not in public.
--
-- relname is matched case-insensitively because Liquibase writes the name uppercase on some
-- databases and lowercase on others, but the value returned keeps the catalog's own spelling:
-- it is later quoted as an identifier, so folding its case would break the query that reads it.
--
-- Every schema is scanned, including ones excluded from the DDL comparison: excluding a schema
-- from the structural diff should not hide the migration history.
SELECT
    n.nspname  AS schema,
    c.relname  AS name,
    lower(c.relname) = 'databasechangeloglock' AS is_lock
FROM pg_catalog.pg_class AS c
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
WHERE c.relkind = 'r'
  AND lower(c.relname) IN ('databasechangelog', 'databasechangeloglock')
  AND n.nspname NOT LIKE 'pg\_%%'
  AND n.nspname <> 'information_schema'
ORDER BY n.nspname, c.relname
