-- The domain now accepts zero, which the master rejects.
ALTER DOMAIN "acme-invoicing".positive_amount DROP CONSTRAINT positive_amount_check;
ALTER DOMAIN "acme-invoicing".positive_amount ADD CONSTRAINT positive_amount_check CHECK (VALUE >= 0);
