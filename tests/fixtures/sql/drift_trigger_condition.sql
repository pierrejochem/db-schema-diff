-- The WHEN clause selects different rows.
DROP TRIGGER invoice_stamp_issued ON "acme-invoicing".invoice;
CREATE TRIGGER invoice_stamp_issued
    BEFORE INSERT OR UPDATE ON "acme-invoicing".invoice
    FOR EACH ROW
    WHEN (NEW.status::text = 'PAID'::text)
    EXECUTE FUNCTION "acme-invoicing".stamp_issued_at();
