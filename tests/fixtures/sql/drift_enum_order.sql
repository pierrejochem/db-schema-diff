-- The same labels in a different order.
--
-- An enum's label order defines its comparison operators, so ORDER BY on that column returns a
-- different sequence here. Real drift, not cosmetic — which is why the label *sequence* is compared
-- rather than the label set.
ALTER TABLE "cumo-invoicing".column_flavours ALTER COLUMN stage  DROP DEFAULT;
ALTER TABLE "cumo-invoicing".column_flavours ALTER COLUMN d_enum DROP DEFAULT;
ALTER TABLE "cumo-invoicing".column_flavours ALTER COLUMN stage  TYPE text;
ALTER TABLE "cumo-invoicing".column_flavours ALTER COLUMN d_enum TYPE text;

DROP TYPE "cumo-invoicing".dunning_stage;
CREATE TYPE "cumo-invoicing".dunning_stage AS ENUM ('NONE', 'SECOND', 'FIRST', 'LEGAL');

ALTER TABLE "cumo-invoicing".column_flavours
    ALTER COLUMN stage TYPE "cumo-invoicing".dunning_stage
    USING stage::"cumo-invoicing".dunning_stage;
ALTER TABLE "cumo-invoicing".column_flavours
    ALTER COLUMN d_enum TYPE "cumo-invoicing".dunning_stage
    USING d_enum::"cumo-invoicing".dunning_stage;
ALTER TABLE "cumo-invoicing".column_flavours ALTER COLUMN d_enum SET DEFAULT 'NONE';
