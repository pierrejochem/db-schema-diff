-- A view absent from the target. open_invoice depends on it, so both go.
DROP VIEW "acme-invoicing".open_invoice;
DROP VIEW "acme-invoicing".invoice_summary;
