-- The same changeset recorded under a different path, same basename.
--
-- This is exactly what relativeToChangelogFile and logicalFilePath produce, and cumo-invoicing's
-- master.xml mixes both forms, so it must not read as a missing changeset. base.sql records this one
-- as 'update_20260115.xml'; here it is the same file reached by a relative include.
UPDATE "cumo-invoicing"."DATABASECHANGELOG"
   SET "FILENAME" = 'liquibase/nested/update_20260115.xml'
 WHERE "ID" = 'add-invoice-line';
