-- A precondition evaluated differently here, usually because the row data differs.
-- cumo-invoicing does this deliberately with onFail="MARK_RAN", so it is a warning worth reading
-- rather than a failed deployment.
UPDATE "cumo-invoicing"."DATABASECHANGELOG"
   SET "EXECTYPE" = 'MARK_RAN'
 WHERE "ID" = 'add-dunning';
