-- One member of an overload set is gone. A caller passing text now gets an error, while a caller
-- passing an integer is unaffected — which is why overloads have to be inventoried separately.
DROP FUNCTION "cumo-invoicing".describe(text);
