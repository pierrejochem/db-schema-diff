-- Which columns a DATABASECHANGELOG table actually has.
--
-- Introspected before reading it, because the table's shape varies by Liquibase major: LABELS
-- arrived in 3.3 and DEPLOYMENT_ID in 3.6. Selecting a column that does not exist fails the whole
-- query, so a comparison between environments running different Liquibase versions would die on
-- the older one rather than reporting that it is behind.
--
-- Names come back in the catalog's own case. Liquibase writes them uppercase on some databases and
-- lowercase on others, and they are later quoted as identifiers, so folding the case here would
-- break the query that reads the rows.
SELECT a.attname AS name
FROM pg_catalog.pg_attribute AS a
JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname = %(schema)s
  AND c.relname = %(table)s
  AND a.attnum > 0
  AND NOT a.attisdropped
ORDER BY a.attnum
