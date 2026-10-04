-- Credentials in definition text, target side. The column default and the function body each carry
-- a hardcoded connection string. Neither may reach any output.
ALTER TABLE "acme-invoicing".invoice
    ADD COLUMN sync_target text DEFAULT 'postgresql://u:s3cret@h/db';

CREATE OR REPLACE FUNCTION "acme-invoicing".invoice_gross(p_invoice_id integer)
RETURNS numeric
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    v_total numeric(14,4);
BEGIN
    PERFORM dblink_connect('postgresql://u:s3cret@h/db');
    SELECT net_amount INTO v_total
      FROM "acme-invoicing".invoice
     WHERE id = p_invoice_id;
    RETURN coalesce(v_total, 0);
END;
$$;

-- An expression index and a function with a SET clause: two more places a literal can sit.
CREATE INDEX idx_invoice_secret ON "acme-invoicing".invoice
    ((number || 'postgresql://u:s3cret@h/db'));

CREATE FUNCTION "acme-invoicing".uses_setting() RETURNS integer
LANGUAGE sql
SET "app.dsn" = 'postgresql://u:s3cret@h/db'
AS 'SELECT 1';

-- A credential in a *view* body. The report now prints the raw definition of a differing body
-- rather than the canonical one-liner, so what keeps this out of the HTML diff is the masking of
-- `raw` text rather than the masking of the compared value. Worth its own fixture for that reason.
CREATE OR REPLACE VIEW "acme-invoicing".open_invoice AS
SELECT * FROM "acme-invoicing".invoice_summary
 WHERE status::text = 'OPEN'::text
   AND 'postgresql://u:s3cret@h/db' <> '';

-- An enum label. Not definition text at all: a label is free text the user wrote, stored bare, so
-- it has to be judged as the literal it came from or it reaches every output verbatim.
ALTER TYPE "acme-invoicing".dunning_stage ADD VALUE 'postgresql://u:s3cret@h/db';
