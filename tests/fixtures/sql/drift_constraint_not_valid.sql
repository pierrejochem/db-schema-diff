-- Added NOT VALID to avoid a long lock, then never validated. The constraint exists but is not
-- enforced for existing rows, which is easy to create by accident and invisible in a name check.
ALTER TABLE "acme-invoicing".invoice DROP CONSTRAINT invoice_net_amount_check;
ALTER TABLE "acme-invoicing".invoice
    ADD CONSTRAINT invoice_net_amount_check CHECK (net_amount >= 0) NOT VALID;
