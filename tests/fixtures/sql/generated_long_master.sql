-- Master side of drift_generated_to_default: the column is a stored generated one whose expression
-- is long enough (>120 characters) that a renderer draws a diff of it rather than a one-line row.
-- That length is what makes the key the raw text is filed under visible: filed as "default", the
-- expression is diffed under `column.default`, whose compared value on this side is empty.
ALTER TABLE "cumo-invoicing".column_flavours
    ADD COLUMN computed_long numeric(14,4) GENERATED ALWAYS AS (
        CASE
            WHEN amount_scaled IS NULL THEN 0::numeric
            WHEN amount_scaled > 1000::numeric THEN amount_scaled * 2::numeric + amount_zero
            ELSE amount_scaled + amount_zero + amount_int
        END
    ) STORED;
