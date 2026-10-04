-- A unique constraint dropped: the target no longer enforces uniqueness.
ALTER TABLE "acme-invoicing".invoice DROP CONSTRAINT invoice_number_key;
