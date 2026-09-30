-- The partial index now covers a different set of rows, so it answers different queries.
DROP INDEX "cumo-invoicing".idx_invoice_open;
CREATE INDEX idx_invoice_open ON "cumo-invoicing".invoice (issued_at)
    WHERE status::text = 'PAID'::text;
