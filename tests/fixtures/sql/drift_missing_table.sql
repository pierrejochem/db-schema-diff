-- A whole table absent from the target: the migration did not land.
-- Expect one ERROR for the table plus one per column.
DROP TABLE "cumo-invoicing"."DunningLevel";
