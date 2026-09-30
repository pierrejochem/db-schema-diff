-- Two changelog tables, in different schemas. Common after a service's liquibase-schema was
-- changed and the old table left behind. Guessing between them would make the report depend on
-- catalog ordering, so the tool asks to be told which one counts.
CREATE TABLE public."DATABASECHANGELOG" (
    "ID"            character varying(255) NOT NULL,
    "AUTHOR"        character varying(255) NOT NULL,
    "FILENAME"      character varying(255) NOT NULL,
    "DATEEXECUTED"  timestamp without time zone NOT NULL,
    "ORDEREXECUTED" integer NOT NULL,
    "EXECTYPE"      character varying(10) NOT NULL,
    "MD5SUM"        character varying(35)
);
