# cumo-db-schema-comparer

Inventory one **master** PostgreSQL database and compare it against **1..n other
environments** of the same service to prove they are in sync — structurally and by Liquibase
changelog state.

Built for the CUMO platform, where ~18 independent Spring Boot services each own a database
on a shared PostgreSQL server and each apply their own Liquibase changelogs across
`rmv-dev` / `rmv-test` / `rmv-qa` / prod. Nothing else answers *"is qa's schema actually the
same as prod's?"* until something fails at runtime.

## Status

Complete against the original plan: all nine phases, verified against PostgreSQL 13, 15 and 17.

Working:

- `compare` — capture a master and 1..n targets in parallel, diff every schema object kind, report to
  the console, JSON and JUnit XML, exit on the documented codes.
- `inventory` / `diff-inventories` / `render` — capture each environment separately and compare or
  re-render offline, so no single process ever holds credentials for two environments at once.
- Normalisation of types, defaults, `serial`/identity/generated columns, dense column ordinals,
  check expressions and index expressions, pinned against real PostgreSQL 15 output rather than
  assumed.
- Constraint-backed indexes deduplicated, so a primary key is reported once rather than twice, and
  renamed auto-named objects reconciled into a single "name differs" warning.
- An ignore ruleset with bundled defaults, scoped by object kind, name glob, target, attribute and
  status — so "tolerate an extra index in dev" cannot also mean "tolerate a missing one".
- Liquibase changelog comparison: which environment is behind, by how many changesets, and where the
  two histories split.
- Generated catalog objects filtered out — internal foreign-key triggers, a table's own row type, a
  range type's constructor functions, an extension's contents — so the report contains only what
  somebody wrote.
- Credentials from the environment only, with a credential type that redacts itself in every
  string context.

Object kinds compared: tables, columns, constraints, indexes, views, materialized views,
sequences, functions, procedures, triggers, enums, domains, composite and range types, and
extensions — plus Liquibase changelog state.

Also: `probe` and `validate-config` for setting a new environment up, `--baseline` for adopting the
tool against environments that have already drifted, and leaf partitions folded into a count so a
production database with sixty monthly partitions does not bury a report.

## Install

Requires Python 3.11 or newer. The interpreter that ships with macOS (3.9) will not work.

```sh
python3.11 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

## Credentials

Credentials **only** come from the environment. The YAML config names targets and points at
environment variable *names*; it never contains a DSN. There is no `--dsn` and no
`--password` flag, by design, so argv and shell history cannot carry a credential.

```sh
export PROD_INVOICING_DSN='postgresql://user:pass@db-prod:5432/invoicing'
export QA_INVOICING_DSN='postgresql://user:pass@db-qa:5432/invoicing'
```

Every source is inspected in a read-only `REPEATABLE READ` transaction with
`statement_timeout` and `lock_timeout` set, so pointing this at production is safe.

## Usage

Start from `config.example.yaml`:

```sh
cp config.example.yaml config/invoicing.yaml
cumo-schema-diff compare -c config/invoicing.yaml
```

`--config` takes a directory too, which runs every service's comparison in one go:

```sh
cumo-schema-diff compare -c config/
```

### Machine-readable output

```sh
# All three reports into one directory, the convenient form for CI.
cumo-schema-diff compare -c config/invoicing.yaml --out-dir build/

# Or name them individually.
cumo-schema-diff compare -c config/invoicing.yaml \
    --json report.json --junit junit.xml --html report.html
```

Point GitHub Actions at `junit.xml` and drift shows up in the Checks tab as ordinary test
failures, naming the schema, table and column.

`report.html` is a **single self-contained file**: inline CSS, inline SVG, no fonts, no CDN, nothing
fetched at all. Upload it as a CI artifact and it opens correctly on a machine with no outbound
network. It leads with the verdict, then an object-kind-by-target matrix, then per-target detail; it
supports dark mode and prints cleanly. Expanding uses `<details>`, so every finding is reachable with
JavaScript disabled — the script only filters by name and severity.

### Comparing environments that share no credentials

Production and QA never have to be reachable from the same place at the same time:

```sh
# In the job that can reach production:
cumo-schema-diff inventory -c config/invoicing.yaml --source prod -o prod.json

# In the job that can reach QA:
cumo-schema-diff inventory -c config/invoicing.yaml --source qa -o qa.json

# Anywhere, with no credentials in the environment at all:
cumo-schema-diff diff-inventories prod.json qa.json --junit junit.xml
```

`render` re-renders a saved JSON report in another format, and reproduces its exit code, so a
later pipeline stage can gate on an earlier stage's comparison without repeating it:

```sh
cumo-schema-diff render --from report.json --junit junit.xml
```

### Liquibase changelog

Read automatically, with no configuration. The table is **located** rather than assumed, because it
is not reliably in `public` — `cumo-invoicing` sets `spring.liquibase.liquibase-schema=cumo-invoicing`
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
  data differs. `cumo-invoicing` does this deliberately with `onFail="MARK_RAN"`, so it is a warning.

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
    liquibase: { schema: cumo-invoicing, table: DATABASECHANGELOG }
```

### Quietening expected differences

Some differences are expected rather than wrong. The bundled ruleset already covers Quartz runtime
state, Liquibase's own tables and conventionally-named scratch objects; add your own on top:

```sh
cumo-schema-diff compare -c config/invoicing.yaml --ignores ignores.yaml
```

Start from `ignores.example.yaml`. A rule can be scoped by object kind, name glob, target label,
attribute and status, and `statuses: [extra_in_target]` is what lets a rule tolerate an extra object
without also tolerating a missing one.

Nothing is hidden without trace. A suppressed finding still appears in the JSON report and as a
skipped case in JUnit, tagged with the rule that suppressed it, and the console says how many were
set aside:

```sh
cumo-schema-diff compare -c config/invoicing.yaml --show-ignored      # list them
cumo-schema-diff compare -c config/invoicing.yaml --no-default-ignores # start from nothing
```

Prefer `action: warn` over `ignore` while something is still being worked on: the finding stays
visible and stops failing an `--fail-on error` gate.

#### Rule keys

| Key | Meaning | Omitted means |
|-----|---------|---------------|
| `id` | Recorded on every finding the rule touches. **Required.** | — |
| `reason` | Why this is acceptable. Optional to the parser, expected by a reviewer. | — |
| `kinds` | Object kinds: `table`, `column`, `constraint`, `index`, `view`, `matview`, `sequence`, `routine`, `trigger`, `enum_type`, `domain_type`, `composite_type`, `range_type`, `extension`, `schema` | any kind |
| `names` | Globs matched against `schema.name` **and** `schema.name.subname`, so one rule covers a table and its columns | any name |
| `attributes` | Dotted `kind.attribute`, e.g. `column.collation`. Makes the rule apply to individual differences rather than whole objects | the rule applies to whole objects |
| `targets` | Target labels | every target |
| `statuses` | `missing_in_target`, `extra_in_target`, `differs` | any status |
| `action` | `ignore` (default), `warn`, `info` | `ignore` |

A rule must restrict at least one of `kinds`, `names`, `attributes` or `statuses`; one that restricts
nothing would suppress the whole report and is refused. Rule ids must be unique, and a project rule
sharing an id with a bundled default replaces it — which is how you turn one default off without
restating the rest.

Two distinctions are worth knowing, because they are what keep a ruleset safe:

- **`statuses` separates "extra" from "missing".** A rule written to tolerate somebody's scratch
  index in dev must not also excuse a missing one, which would be a failed deployment.
- **`attributes` scopes a rule to one difference.** `attributes: [column.collation]` accepts a
  collation mismatch while still reporting that same column being retyped or dropped.

### Useful flags

| Flag | Effect |
|------|--------|
| `--target qa` | Compare only these targets. Repeatable. |
| `--fail-on warning` | Lower the gate. `error` (default), `warning`, `any`, `never`. |
| `--allow-unreachable` | Let the reachable targets decide the verdict. Still exits 3 if none was reachable. |
| `--exclude-schema quartz` | Leave a schema out. Repeatable. |
| `--show-cosmetic` | Show the differences that normalise away — useful for auditing this tool. |
| `--ignores PATH` | Layer a ruleset over the bundled defaults. |
| `--no-default-ignores` | Suppress nothing by default. |
| `--show-ignored` | List what the rules suppressed, and which rule did it. |
| `--html PATH` | Write the standalone HTML report. |
| `--out-dir DIR` | Write all three machine-readable reports into one directory. |
| `--sequential` | Capture one source at a time, for debugging. |

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | In sync at or above the configured `--fail-on` severity |
| 1 | Drift found — the CI gate |
| 2 | Unusable input: bad YAML, missing env var, unsupported server version |
| 3 | A source could not be inspected (connect / auth / permission / timeout) |
| 4 | Internal error — a bug in this tool |
| 130 | Interrupted |

Exit **3 takes precedence over 1**: a partial comparison is never reported as a clean gate.

## A worked example: the RMV stack

`cumo-local-qa-env/RMV` runs one PostgreSQL holding a database per service. This walks through
pointing the comparer at it and reading what comes back.

Bring the stack up, then create a config per service database. Start with `invoicing`, because it is
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
export PROD_INVOICING_DSN='postgresql://cumo:...@db-prod.internal:5432/invoicing'
export LOCAL_INVOICING_DSN='postgresql://postgres:...@localhost:5432/invoicing'
```

**First, check the config without connecting.** This runs fine in a pipeline stage before any
database is reachable:

```sh
cumo-schema-diff validate-config -c config/invoicing.yaml --check-env
```

**Then probe.** This answers the questions that otherwise turn into a confusing comparison — which
server version, which schemas, where the changelog actually lives, and whether the connecting role
can see the definitions at all:

```
$ cumo-schema-diff probe -c config/invoicing.yaml
prod: PostgreSQL 15.19
  database  invoicing as cumo
  encoding  UTF8  collation de_DE.utf8
  schemas   cumo-invoicing, public
  objects   41 tables, 380 columns, 52 constraints, 28 indexes, 3 views, ...
  liquibase cumo-invoicing.DATABASECHANGELOG: 214 changeset(s), last tag R7.6.2
```

If `probe` reports `liquibase AMBIGUOUS`, name the right table in the config:

```yaml
    liquibase: { schema: cumo-invoicing, table: DATABASECHANGELOG }
```

If it warns that a role returned no definition for some views, fix the grant before trusting a
comparison — otherwise those objects read as drift when they are really a permissions problem.

**Now compare.** The first run against environments that have drifted for years will find a lot,
all of it true and none of it actionable today:

```sh
cumo-schema-diff compare -c config/invoicing.yaml --json baseline.json
```

Review that report, and once it reflects the backlog you have decided to live with, keep it:

```sh
cumo-schema-diff compare -c config/invoicing.yaml --baseline baseline.json --out-dir build/
```

From here the run fails only on findings the baseline does not contain, so the backlog stays visible
and stops blocking while anything *new* is caught from day one. Accepted findings still appear in
`report.json` and as skipped cases in `junit.xml`, tagged `baseline` — nothing is hidden.

When something in the backlog gets fixed, the run says so (`3 no longer occur`), and the baseline is
worth regenerating smaller.

**Point `--config` at the directory** to do every service at once, each into its own subdirectory:

```sh
cumo-schema-diff compare -c config/ --out-dir build/
```

A baseline is scoped to the report it came from, so `--baseline` belongs with a single config. Run
each service with its own baseline when you need them.

### Reading the exit code in CI

| Code | What to do |
|------|------------|
| 0 | Nothing. |
| 1 | Real drift at or above the gate. Read `report.html`. |
| 2 | Bad config or a missing environment variable. Fix and re-run. |
| 3 | A database could not be inspected. **Retry** — this is usually transient, and it is deliberately not the same as drift. |

## Development

```sh
make test               # unit tests, no Docker needed
make test-integration   # integration tests, needs Docker
make lint               # ruff format --check, ruff check, mypy
```

The integration suite runs against a throwaway container. To run it against another server version,
or against an existing one:

```sh
CUMO_SCHEMA_DIFF_TEST_IMAGE=postgres:17 make test-integration
CUMO_SCHEMA_DIFF_TEST_DSN='postgresql://...' make test-integration
```

Two tests are worth knowing about:

- **`tests/integration/test_no_drift.py`** applies the same DDL to two databases and asserts *zero*
  findings. Almost every normalisation bug shows up there first. If it is green the tool can be
  trusted on real environments; if it is red nothing else matters.
- **`tests/integration/test_cross_version.py`** builds the same schema on PostgreSQL 13 and 17 and
  asserts nothing structural differs. That is what catches the version-dependent catalog spellings.
