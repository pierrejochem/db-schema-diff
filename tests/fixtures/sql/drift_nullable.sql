-- A NOT NULL dropped. Expect one ERROR on column.is_nullable.
ALTER TABLE "cumo-invoicing".invoice ALTER COLUMN number DROP NOT NULL;
