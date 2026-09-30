-- The target never received the last two migrations. The commonest real case, and the one the
-- report should lead with: "qa is 2 changesets behind prod".
DELETE FROM "cumo-invoicing"."DATABASECHANGELOG" WHERE "ORDEREXECUTED" >= 2;
