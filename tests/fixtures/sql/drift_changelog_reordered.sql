-- The same changesets applied in a different order, with a different per-deployment counter base.
-- The counter itself must not be compared; the relative order must.
UPDATE "cumo-invoicing"."DATABASECHANGELOG" SET "ORDEREXECUTED" = "ORDEREXECUTED" + 100;
