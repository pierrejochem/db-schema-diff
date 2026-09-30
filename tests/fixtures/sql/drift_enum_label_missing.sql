-- The enum is missing its last label, so a value the master accepts is rejected here.
--
-- PostgreSQL cannot remove an enum label, so the type has to be recreated. Two columns use it
-- (`stage` and `d_enum`), and both have to be detached and reattached — a reminder that an enum is
-- rarely used in only one place, which is why a label change is an error rather than a detail.
ALTER TABLE "cumo-invoicing".column_flavours ALTER COLUMN stage  DROP DEFAULT;
ALTER TABLE "cumo-invoicing".column_flavours ALTER COLUMN d_enum DROP DEFAULT;
ALTER TABLE "cumo-invoicing".column_flavours ALTER COLUMN stage  TYPE text;
ALTER TABLE "cumo-invoicing".column_flavours ALTER COLUMN d_enum TYPE text;

DROP TYPE "cumo-invoicing".dunning_stage;
CREATE TYPE "cumo-invoicing".dunning_stage AS ENUM ('NONE', 'FIRST', 'SECOND');

ALTER TABLE "cumo-invoicing".column_flavours
    ALTER COLUMN stage TYPE "cumo-invoicing".dunning_stage
    USING stage::"cumo-invoicing".dunning_stage;
ALTER TABLE "cumo-invoicing".column_flavours
    ALTER COLUMN d_enum TYPE "cumo-invoicing".dunning_stage
    USING d_enum::"cumo-invoicing".dunning_stage;
ALTER TABLE "cumo-invoicing".column_flavours ALTER COLUMN d_enum SET DEFAULT 'NONE';
