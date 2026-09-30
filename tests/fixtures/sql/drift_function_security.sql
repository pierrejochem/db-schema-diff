-- SECURITY DEFINER dropped and the pinned search_path removed. Both are privilege-relevant: the
-- function now runs as the caller and resolves names through whatever search_path they happen to have.
CREATE OR REPLACE FUNCTION "cumo-invoicing".mandant_count() RETURNS bigint
    LANGUAGE sql
    SECURITY INVOKER
    AS $$ SELECT count(*) FROM public.mandant $$;
