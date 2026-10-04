-- The materialized view became a plain view. It no longer holds data and no longer needs refreshing,
-- which changes both performance and staleness behaviour.
DROP MATERIALIZED VIEW "acme-invoicing".invoice_totals;
CREATE VIEW "acme-invoicing".invoice_totals AS
SELECT mandant_id, count(*) AS invoice_count, sum(net_amount) AS net_total
  FROM "acme-invoicing".invoice
 GROUP BY mandant_id;
