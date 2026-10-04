-- The view gained a column. Anything doing SELECT * through it now sees something different.
CREATE OR REPLACE VIEW "acme-invoicing".invoice_summary AS
SELECT i.id, i.number, i.mandant_id, m.name AS mandant_name,
       i.net_amount, i.gross_amount, i.status, i.tax_rate
  FROM "acme-invoicing".invoice AS i
  JOIN public.mandant AS m ON m.id = i.mandant_id;
