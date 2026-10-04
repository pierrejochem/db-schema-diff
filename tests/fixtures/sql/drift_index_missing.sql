-- A non-unique index absent from the target. Costs performance, not correctness.
DROP INDEX "acme-invoicing".idx_invoice_open;
