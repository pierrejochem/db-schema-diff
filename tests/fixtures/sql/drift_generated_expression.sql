-- The stored generated column computes something different. PostgreSQL cannot alter a generation
-- expression in place, so the column is dropped and re-added; that also moves it to the end of the
-- table, which is a second (ordinal) difference on the same column and does not matter here.
--
-- The point of the fixture: a stored generated column's expression lives in the same catalog slot
-- as a default, so filing the server's text under the wrong key makes the report print a generation
-- expression beside an empty `column.default`.
ALTER TABLE "cumo-invoicing".column_flavours DROP COLUMN computed;
ALTER TABLE "cumo-invoicing".column_flavours
    ADD COLUMN computed numeric(14,4) GENERATED ALWAYS AS (amount_scaled * 3) STORED;
