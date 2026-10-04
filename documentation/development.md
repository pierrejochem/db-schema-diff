---
title: "Development and testing"
description: "Run the DB Schema Diff test suites: unit tests, integration tests against PostgreSQL 11, 13, 15 and 17 in containers, and the checks that matter most."
---

# Development

```sh
make test               # unit tests, no Docker needed
make test-integration   # integration tests, needs Docker
make lint               # ruff format --check, ruff check, mypy
```

The integration suite runs against a throwaway container. To run it against another server version,
or against an existing one:

```sh
DB_SCHEMA_DIFF_TEST_IMAGE=postgres:17 make test-integration
DB_SCHEMA_DIFF_TEST_DSN='postgresql://...' make test-integration
```

> **`DB_SCHEMA_DIFF_TEST_DSN` is destructive.** The suite runs
> `DROP DATABASE IF EXISTS … WITH (FORCE)` and recreates `master_db` and `target_db` on whatever
> server that DSN points at, once per session, forcing any other connection to them off. Point it
> only at a server you own, and never at one that hosts anything called `master_db` or `target_db`
> that you want to keep. The default path — a throwaway container — touches nothing.

Two tests are worth knowing about:

- **`tests/integration/test_no_drift.py`** applies the same DDL to two databases and asserts *zero*
  findings. Almost every normalisation bug shows up there first. If it is green the tool can be
  trusted on real environments; if it is red nothing else matters.
- **`tests/integration/test_cross_version.py`** builds the same schema on PostgreSQL 13 and 17 and
  asserts nothing structural differs. That is what catches the version-dependent catalog spellings.
- **`tests/integration/test_pg11.py`** does the same on two PostgreSQL 11s, and asserts every
  object kind is still captured there. A supported floor that nothing runs against is a guess.
