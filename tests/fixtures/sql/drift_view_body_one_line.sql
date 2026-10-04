-- One line of a nine-line view body changes: the join becomes outer, so the view now also returns
-- invoices whose mandant row is gone. Everything above that line is identical, which is the point
-- — a rendered diff of this has to show the surrounding lines as context rather than replacing the
-- whole definition.
CREATE OR REPLACE VIEW "acme-invoicing".invoice_summary AS
SELECT i.id,
       i.number,
       i.mandant_id,
       m.name AS mandant_name,
       i.net_amount,
       i.gross_amount,
       i.status
  FROM "acme-invoicing".invoice AS i
  LEFT JOIN public.mandant AS m ON m.id = i.mandant_id;
