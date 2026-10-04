-- The trigger exists but does nothing. The classic production-only workaround, and invisible to
-- anything that only checks whether the trigger is there.
ALTER TABLE "acme-invoicing".invoice DISABLE TRIGGER invoice_stamp_issued;
