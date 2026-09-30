-- A changeset that failed. Whatever it was meant to do did not happen.
UPDATE "cumo-invoicing"."DATABASECHANGELOG"
   SET "EXECTYPE" = 'FAILED'
 WHERE "ID" = 'add-dunning';
