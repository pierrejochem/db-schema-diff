-- One column widened. Expect exactly one ERROR on column.data_type.
ALTER TABLE "cumo-invoicing".invoice_line ALTER COLUMN position TYPE integer;
