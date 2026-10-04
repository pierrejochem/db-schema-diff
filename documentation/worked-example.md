---
title: "Worked example: compare a service database across environments"
description: "A complete walkthrough: write a config, validate it without connecting, probe the servers, compare, adopt a baseline for years of drift, and run every service at once."
---

# A worked example

Take a PostgreSQL server that holds one database per service. This walks through comparing one
service's database across environments and reading what comes back.

Create a config per service database. Start with `invoicing`, because it is
the awkward one — its Liquibase changelog lives in a hyphenated schema of its own:

```yaml
# config/invoicing.yaml
version: 1
name: invoicing
master:
  label: prod
  host: db-prod.internal
  database: invoicing
  dsn_env: PROD_INVOICING_DSN
targets:
  - label: local
    dsn_env: LOCAL_INVOICING_DSN
exclude_schemas: [quartz]
```

Credentials come only from the environment:

```sh
export PROD_INVOICING_DSN='postgresql://app:...@db-prod.internal:5432/invoicing'
export LOCAL_INVOICING_DSN='postgresql://postgres:...@localhost:5432/invoicing'
```

**First, check the config without connecting.** This runs fine in a pipeline stage before any
database is reachable:

```sh
db-schema-diff validate-config -c config/invoicing.yaml --check-env
```

**Then probe.** This answers the questions that otherwise turn into a confusing comparison — which
server version, which schemas, and where the changelog actually lives:

```
$ db-schema-diff probe -c config/invoicing.yaml
prod: PostgreSQL 15.19
  database  invoicing as app
  encoding  UTF8  collation de_DE.utf8
  schemas   acme-invoicing, public
  liquibase acme-invoicing.DATABASECHANGELOG: 214 changeset(s), last tag R7.6.2
```

If `probe` reports `liquibase AMBIGUOUS`, name the right table in the config:

```yaml
    liquibase: { schema: acme-invoicing, table: DATABASECHANGELOG }
```

**Now compare.** The first run against environments that have drifted for years will find a lot,
all of it true and none of it actionable today:

```sh
db-schema-diff compare -c config/invoicing.yaml --json baseline.json
```

Review that report, and once it reflects the backlog you have decided to live with, keep it:

```sh
db-schema-diff compare -c config/invoicing.yaml --baseline baseline.json --out-dir build/
```

From here the run fails only on findings the baseline does not contain, so the backlog stays visible
and stops blocking while anything *new* is caught from day one. Accepted findings still appear in
`report.json` and as skipped cases in `junit.xml`, tagged `baseline` — nothing is hidden.

When something in the backlog gets fixed, the run says so (`3 no longer occur`), and the baseline is
worth regenerating smaller.

**Point `--config` at the directory** to do every service at once, each into its own subdirectory:

```sh
db-schema-diff compare -c config/ --out-dir build/
```

A baseline is scoped to the report it came from, so `--baseline` belongs with a single config. Run
each service with its own baseline when you need them.
