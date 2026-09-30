-- Identity of the connected server and database.
--
-- datcollate and datctype matter beyond documentation: when two databases disagree about
-- their collation, every text column's collation differs by definition, so the comparison
-- suppresses that attribute and says so once rather than reporting thousands of findings.
SELECT
    current_database()                             AS database,
    current_user                                   AS "user",
    current_setting('server_version_num')::int     AS server_version_num,
    current_setting('server_version')              AS server_version,
    pg_encoding_to_char(d.encoding)                AS encoding,
    d.datcollate                                   AS datcollate,
    d.datctype                                     AS datctype,
    inet_server_addr()::text                       AS server_addr
FROM pg_catalog.pg_database AS d
WHERE d.datname = current_database()
