-- The sequence has issued values here and not there. This is runtime state, not schema: a production
-- sequence is always ahead of a QA one. Must report nothing.
SELECT setval('"cumo-invoicing".reference_seq', 5000);
SELECT nextval('"cumo-invoicing".invoice_id_seq');
SELECT nextval('"cumo-invoicing".invoice_id_seq');
