-- No Liquibase bookkeeping at all in the target. Either nothing was ever deployed, or the table
-- lives in a schema the tool was not told about — which is why the report suggests liquibase.schema.
DROP TABLE "cumo-invoicing"."DATABASECHANGELOG";
