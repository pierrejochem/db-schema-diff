-- One column widened. Expect exactly one ERROR on column.data_type.
ALTER TABLE "acme-invoicing".invoice_line ALTER COLUMN position TYPE integer;
