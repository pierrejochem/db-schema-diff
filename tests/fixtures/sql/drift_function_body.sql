-- The function computes something different.
CREATE OR REPLACE FUNCTION "acme-invoicing".invoice_gross(p_invoice_id integer)
RETURNS numeric
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    v_total numeric(14,4);
BEGIN
    SELECT net_amount INTO v_total
      FROM "acme-invoicing".invoice
     WHERE id = p_invoice_id;
    RETURN coalesce(v_total, 0);
END;
$$;
