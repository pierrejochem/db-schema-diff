-- The target has more partitions, because its maintenance job has run more often. Expected, not
-- drift: reported at INFO so it is visible without failing anything.
CREATE TABLE "acme-invoicing".invoice_event_2026_03
    PARTITION OF "acme-invoicing".invoice_event
    FOR VALUES FROM ('2026-03-01') TO ('2026-04-01');
CREATE TABLE "acme-invoicing".invoice_event_2026_04
    PARTITION OF "acme-invoicing".invoice_event
    FOR VALUES FROM ('2026-04-01') TO ('2026-05-01');
