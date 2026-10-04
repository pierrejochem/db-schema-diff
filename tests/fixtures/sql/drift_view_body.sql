-- Same columns, different query behind them. The view now returns a different set of rows.
CREATE OR REPLACE VIEW "acme-invoicing".open_invoice AS
SELECT * FROM "acme-invoicing".invoice_summary WHERE status::text = 'PAID'::text;
