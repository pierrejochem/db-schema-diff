-- Quartz runtime state absent from the target. Suppressed by the bundled ruleset: these tables
-- record which node holds which lock, not what the application's schema looks like.
DROP TABLE public."QRTZ_LOCKS";
