-- The same schema, spelled differently where it genuinely survives into the catalog.
--
-- Verified against PostgreSQL 15: the server canonicalises *type* spellings itself (int4 is
-- stored as integer, varchar as character varying), so writing a type two ways produces no
-- catalog difference at all. Default *expressions* are different: pg_attrdef keeps now() and
-- CURRENT_TIMESTAMP as written, so two environments whose migrations spelled the same default
-- differently disagree here forever.
--
-- A comparer that string-compares defaults reports drift on this file. This one must report
-- nothing.
ALTER TABLE public.mandant        ALTER COLUMN created_at SET DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE "cumo-invoicing".invoice ALTER COLUMN issued_at SET DEFAULT now();
-- Redundant parentheses and an explicit cast to the column's own type: both are noise the
-- server prints back, and neither changes the value.
ALTER TABLE "cumo-invoicing".invoice ALTER COLUMN net_amount SET DEFAULT (0)::numeric;
ALTER TABLE "cumo-invoicing".invoice ALTER COLUMN status     SET DEFAULT 'DRAFT';
