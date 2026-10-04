-- The trigger no longer fires on UPDATE, so half the rows it used to stamp now go unstamped.
DROP TRIGGER invoice_stamp_issued ON "acme-invoicing".invoice;
CREATE TRIGGER invoice_stamp_issued
    BEFORE INSERT ON "acme-invoicing".invoice
    FOR EACH ROW
    WHEN (NEW.status::text = 'OPEN'::text)
    EXECUTE FUNCTION "acme-invoicing".stamp_issued_at();
