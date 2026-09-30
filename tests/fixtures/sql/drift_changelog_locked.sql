-- A deployment lock is held, so the snapshot may have been taken mid-migration. Worth saying before
-- anybody trusts the rest of the report.
UPDATE "cumo-invoicing"."DATABASECHANGELOGLOCK" SET "LOCKED" = true WHERE "ID" = 1;
