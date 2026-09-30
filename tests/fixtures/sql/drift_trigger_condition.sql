-- The WHEN clause selects different rows.
DROP TRIGGER invoice_stamp_issued ON "cumo-invoicing".invoice;
CREATE TRIGGER invoice_stamp_issued
    BEFORE INSERT OR UPDATE ON "cumo-invoicing".invoice
    FOR EACH ROW
    WHEN (NEW.status::text = 'PAID'::text)
    EXECUTE FUNCTION "cumo-invoicing".stamp_issued_at();
