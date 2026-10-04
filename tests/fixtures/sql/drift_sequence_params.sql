-- The sequence now produces a different series and stops cycling.
ALTER SEQUENCE "acme-invoicing".reference_seq INCREMENT BY 1 MAXVALUE 500000 NO CYCLE;
