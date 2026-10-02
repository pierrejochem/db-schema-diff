-- Target side: the same column, but a plainly defaulted one rather than a generated one. So
-- `column.default` differs (absent against a default) and `column.generated` differs (an
-- expression against nothing) on the same column, which is what puts the generation expression and
-- the default side by side in one report.
ALTER TABLE "cumo-invoicing".column_flavours
    ADD COLUMN computed_long numeric(14,4) DEFAULT 7.5;
