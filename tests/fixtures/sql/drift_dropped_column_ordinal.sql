-- The target reaches the same final shape by a different route: it never had the extra column,
-- so its attnum values have no hole while the master's do. The final schemas are identical, so
-- a comparer using a dense ordinal reports nothing and one using attnum reports every column
-- after the hole.
DROP TABLE public.audit_entry;
CREATE TABLE public.audit_entry (
    id           integer PRIMARY KEY,
    actor        character varying(64) NOT NULL,
    happened_at  timestamp with time zone NOT NULL DEFAULT now(),
    payload      text
);
