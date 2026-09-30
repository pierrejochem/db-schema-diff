-- The same function, reformatted. Indentation and blank lines are not behaviour, so this must report
-- nothing.
CREATE OR REPLACE FUNCTION "cumo-invoicing".invoice_gross(p_invoice_id integer)
RETURNS numeric
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
        v_total   numeric(14,4);
BEGIN


        SELECT gross_amount   INTO v_total
          FROM "cumo-invoicing".invoice
         WHERE id = p_invoice_id;
        IF v_total IS NULL THEN
                RAISE EXCEPTION 'Invoice % not found', p_invoice_id;
        END IF;
        RETURN v_total;
END;
$$;
