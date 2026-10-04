-- A precondition evaluated differently here, usually because the row data differs.
-- acme-invoicing does this deliberately with onFail="MARK_RAN", so it is a warning worth reading
-- rather than a failed deployment.
UPDATE "acme-invoicing"."DATABASECHANGELOG"
   SET "EXECTYPE" = 'MARK_RAN'
 WHERE "ID" = 'add-dunning';
