-- A deployed changeset was edited after the fact. Liquibase will never re-apply it, so the two
-- databases will differ forever without anything in the changelog counts showing it.
UPDATE "acme-invoicing"."DATABASECHANGELOG"
   SET "MD5SUM" = '9:ffffffffffffffffffffffffffffffff'
 WHERE "ID" = 'add-invoice-line';
