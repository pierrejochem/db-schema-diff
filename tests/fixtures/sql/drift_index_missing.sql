-- A non-unique index absent from the target. Costs performance, not correctness.
DROP INDEX "cumo-invoicing".idx_invoice_open;
