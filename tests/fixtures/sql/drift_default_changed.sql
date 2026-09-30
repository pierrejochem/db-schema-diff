-- A default changed. Expect one WARNING on column.default.
ALTER TABLE "cumo-invoicing".invoice ALTER COLUMN status SET DEFAULT 'OPEN'::character varying;
