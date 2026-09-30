-- The check now accepts a value the master rejects.
ALTER TABLE "cumo-invoicing".invoice DROP CONSTRAINT invoice_net_amount_check;
ALTER TABLE "cumo-invoicing".invoice
    ADD CONSTRAINT invoice_net_amount_check CHECK (net_amount >= (-100)::numeric);
