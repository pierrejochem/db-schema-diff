-- The master side of drift_secret_default: same column, a different credential, so the default
-- differs and the masked values have to differ too.
ALTER TABLE "cumo-invoicing".invoice
    ADD COLUMN sync_target text DEFAULT 'postgresql://u:other@h/db';
