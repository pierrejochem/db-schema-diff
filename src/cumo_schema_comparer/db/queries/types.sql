-- Enum, domain, composite and range types.
--
-- Two filters here each prevent a flood.
--
-- 1. Every table creates a composite type of the same name. Without the relkind = 'c' test, the
--    composite-type section mirrors the table section exactly, doubling the whole report.
--
-- 2. Extension-owned types are excluded; the extension's version is compared instead.
--
-- 3. Multirange types (typtype 'm') are excluded. PostgreSQL 14+ creates one automatically for every
--    range type, so it can never differ without its range type differing first, and reporting both
--    doubles the finding.
--
-- Enum labels come back ordered by enumsortorder but the *values* are never compared: they become
-- fractional after ALTER TYPE ... ADD VALUE BEFORE, so two databases holding the same labels in the
-- same order can disagree about the numbers. The order itself matters, because it defines the type's
-- comparison operators.
SELECT
    n.nspname                                              AS schema,
    t.typname                                              AS name,
    t.typtype                                              AS typtype,
    labels.names                                           AS labels,
    CASE
        WHEN t.typtype = 'd' THEN pg_catalog.format_type(t.typbasetype, t.typtypmod)
        WHEN t.typtype = 'r' THEN pg_catalog.format_type(r.rngsubtype, NULL)
    END                                                    AS base_type,
    t.typnotnull                                           AS not_null,
    t.typdefault                                           AS default_value,
    checks.definitions                                     AS constraints,
    attrs.definitions                                      AS attributes
FROM pg_catalog.pg_type AS t
JOIN pg_catalog.pg_namespace AS n ON n.oid = t.typnamespace
LEFT JOIN pg_catalog.pg_class AS rel ON rel.oid = t.typrelid
LEFT JOIN pg_catalog.pg_range AS r ON r.rngtypid = t.oid
LEFT JOIN LATERAL (
    SELECT array_agg(e.enumlabel ORDER BY e.enumsortorder) AS names
    FROM pg_catalog.pg_enum AS e
    WHERE e.enumtypid = t.oid
) AS labels ON true
LEFT JOIN LATERAL (
    SELECT array_agg(pg_catalog.pg_get_constraintdef(con.oid, true) ORDER BY con.conname)
               AS definitions
    FROM pg_catalog.pg_constraint AS con
    WHERE con.contypid = t.oid
) AS checks ON true
LEFT JOIN LATERAL (
    SELECT array_agg(
               a.attname || ' ' || pg_catalog.format_type(a.atttypid, a.atttypmod)
               ORDER BY a.attnum
           ) AS definitions
    FROM pg_catalog.pg_attribute AS a
    WHERE a.attrelid = t.typrelid AND a.attnum > 0 AND NOT a.attisdropped
) AS attrs ON true
WHERE t.typtype IN ('e', 'd', 'c', 'r')
  AND n.nspname = ANY (%(schemas)s::text[])
  -- A composite type only counts when it is a standalone one, not a table's row type.
  AND (t.typtype <> 'c' OR rel.relkind = 'c')
  -- An array type is an implementation detail of its element type.
  AND t.typcategory <> 'A'
  AND NOT EXISTS (
      SELECT 1
      FROM pg_catalog.pg_depend AS dep
      WHERE dep.objid = t.oid
        AND dep.classid = 'pg_catalog.pg_type'::regclass
        AND dep.deptype = 'e'
  )
ORDER BY n.nspname, t.typname
