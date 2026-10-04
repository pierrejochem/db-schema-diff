-- A default changed. Expect one WARNING on column.default.
ALTER TABLE "acme-invoicing".invoice ALTER COLUMN status SET DEFAULT 'OPEN'::character varying;
