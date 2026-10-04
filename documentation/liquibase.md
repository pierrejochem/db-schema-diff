---
title: "Compare Liquibase changelog state across databases"
description: "DB Schema Diff reads DATABASECHANGELOG automatically and reports which environment is behind, by how many changesets, and where the two histories split."
---

## Liquibase changelog

Read automatically, with no configuration. The table is **located** rather than assumed, because it
is not reliably in `public` — `acme-invoicing` sets `spring.liquibase.liquibase-schema=acme-invoicing`
and keeps it in a quoted, hyphenated schema of its own.

```
liquibase
  qa is 2 changeset(s) behind prod (first missing: add-invoice-line by kolowae)
  prod @ R7.6.2 · qa @ untagged
  histories diverge at: add-invoice-line by kolowae
```

`histories diverge at` is the actionable line: it names where the two histories split rather than
listing every consequence downstream of the split.

A changeset's identity is `(ID, AUTHOR)` and deliberately excludes `FILENAME`, because
`relativeToChangelogFile` and `logicalFilePath` make the same changeset record different filenames in
different deployments. `ORDEREXECUTED` is a per-deployment counter and is never compared numerically;
only the relative order of changesets present in both is.

Two differences that look alarming and are not:

- **A Liquibase upgrade rewrites every checksum.** The algorithm-version prefix (`8:`, `9:`) is
  compared separately, so an upgrade is one warning rather than one error per changeset.
- **`EXECUTED` vs `MARK_RAN`** means a precondition evaluated differently, usually because the row
  data differs. `acme-invoicing` does this deliberately with `onFail="MARK_RAN"`, so it is a warning.

| Flag | Effect |
|------|--------|
| `--skip-liquibase` | Do not read `DATABASECHANGELOG` at all. |
| `--strict-changelog` | Also compare `COMMENTS`, `CONTEXTS` and `LABELS`. |

If several changelog tables exist, the run reports the candidates and asks to be told which counts —
guessing would make the answer depend on catalog ordering. Name it in the config:

```yaml
targets:
  - label: qa
    dsn_env: QA_INVOICING_DSN
    liquibase: { schema: acme-invoicing, table: DATABASECHANGELOG }
```
