-- A unique index downgraded to a plain one: the target no longer enforces the constraint.
DROP INDEX "cumo-invoicing".idx_invoice_number_include;
CREATE INDEX idx_invoice_number_include
    ON "cumo-invoicing".invoice (number) INCLUDE (mandant_id);
