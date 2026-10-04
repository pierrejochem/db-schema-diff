-- Liquibase was upgraded in this environment and rewrote every checksum with a new algorithm
-- version. A comparer that string-compares checksums reports every changeset in the database as
-- edited; the real answer is one note saying the algorithms differ.
UPDATE "acme-invoicing"."DATABASECHANGELOG"
   SET "MD5SUM" = '8:' || md5("ID");
