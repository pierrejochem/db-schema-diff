-- Somebody added an index in a lower environment while investigating a slow query.
CREATE INDEX idx_invoice_note ON "cumo-invoicing".invoice (note);
