-- ON DELETE CASCADE became RESTRICT. Deleting an invoice now fails instead of removing its lines.
ALTER TABLE "cumo-invoicing".invoice_line
    DROP CONSTRAINT invoice_line_invoice_id_fkey;
ALTER TABLE "cumo-invoicing".invoice_line
    ADD CONSTRAINT invoice_line_invoice_id_fkey
    FOREIGN KEY (invoice_id) REFERENCES "cumo-invoicing".invoice (id)
    ON UPDATE CASCADE ON DELETE RESTRICT;
