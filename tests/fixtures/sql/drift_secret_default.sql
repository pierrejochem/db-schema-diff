-- Credentials in definition text, target side. The column default and the function body each carry
-- a hardcoded connection string. Neither may reach any output.
ALTER TABLE "cumo-invoicing".invoice
    ADD COLUMN sync_target text DEFAULT 'postgresql://u:s3cret@h/db';

CREATE OR REPLACE FUNCTION "cumo-invoicing".invoice_gross(p_invoice_id integer)
RETURNS numeric
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    v_total numeric(14,4);
BEGIN
    PERFORM dblink_connect('postgresql://u:s3cret@h/db');
    SELECT net_amount INTO v_total
      FROM "cumo-invoicing".invoice
     WHERE id = p_invoice_id;
    RETURN coalesce(v_total, 0);
END;
$$;
