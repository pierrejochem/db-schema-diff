-- A NOT NULL dropped. Expect one ERROR on column.is_nullable.
ALTER TABLE "acme-invoicing".invoice ALTER COLUMN number DROP NOT NULL;
