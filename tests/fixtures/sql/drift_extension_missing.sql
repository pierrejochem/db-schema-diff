-- An extension absent from the target. Everything it provides is missing with it, which is why the
-- extension is compared as a unit rather than by its hundreds of functions.
DROP EXTENSION pgcrypto CASCADE;
