-- A view absent from the target. open_invoice depends on it, so both go.
DROP VIEW "cumo-invoicing".open_invoice;
DROP VIEW "cumo-invoicing".invoice_summary;
