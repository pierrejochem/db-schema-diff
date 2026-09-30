-- The same trigger written with its events in the other order. Identical behaviour, so this must
-- report nothing.
DROP TRIGGER invoice_stamp_issued ON "cumo-invoicing".invoice;
CREATE TRIGGER invoice_stamp_issued
    BEFORE UPDATE OR INSERT ON "cumo-invoicing".invoice
    FOR EACH ROW
    WHEN (NEW.status::text = 'OPEN'::text)
    EXECUTE FUNCTION "cumo-invoicing".stamp_issued_at();
