-- Same index, different name. The name was chosen by PostgreSQL, so two environments built by
-- slightly different migration paths legitimately disagree about it.
--
-- Expect ONE warning saying the name differs — not a missing index plus an extra one.
ALTER INDEX "acme-invoicing".idx_invoice_mandant RENAME TO invoice_mandant_id_idx;
