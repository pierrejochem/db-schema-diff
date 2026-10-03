<img src="media/logo.svg" alt="CUMO Schema Diff" width="390">

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

### Standalone executables

For a machine with no Python at all, Nuitka compiles either entry point into a single file:

```sh
make exe        # the desktop application
make exe-cli    # build/cumo-schema-diff — the command-line tool, one file
```

On macOS `make exe` produces `build/cumo-schema-diff-gui.app`, a real bundle: that is the only way
to get `NSHighResolutionCapable`, without which a Slint window renders non-retina, and it is what
you drag to `/Applications`. The bundle is built `--standalone` rather than `--onefile` because with
`--onefile` Nuitka 4.2 writes `Info.plist` beside the bundle instead of inside `Contents/` and names
a `CFBundleExecutable` that is not there, so the result does not launch. It is **not** code signed
or notarized, so Gatekeeper will warn anyone who did not build it themselves. On other platforms,
and for the command-line tool everywhere, the output is a single file.

Each target installs Nuitka on demand (the `exe` extra, deliberately out of `dev`: no test or CI
job compiles anything) and needs a C toolchain — Xcode command line tools on macOS. A build takes
several minutes.

The build passes `--include-package-data=cumo_schema_comparer`, which is not optional. Everything
this program reads at run time is package data loaded through `importlib.resources`: the catalog
queries, the report templates, the bundled ignore ruleset, the `.slint` markup and the typefaces.
Nuitka ships none of it by default, so without that flag the build succeeds and the binary fails on
the first query it tries to load. There are two entry scripts rather than one because Nuitka
compiles a script into a binary, and the two programs differ: the GUI needs Python 3.12 or newer
for Slint, while the command-line tool keeps its 3.11 floor.

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

### Reaching a database through a bastion

A source that is not directly routable gets an `ssh:` block, and the tunnel is opened for exactly as
long as the connection it serves — by the CLI and the desktop application alike:

```yaml
  - label: qa
    dsn_env: QA_INVOICING_DSN
    ssh:
      host: bastion-qa.internal
      user: deploy
      private_key: ~/.ssh/id_ed25519
      passphrase_env: QA_SSH_KEY_PASSPHRASE   # omit it if your agent has the key
```

The DSN names the database **as the gateway sees it**, so one DSN is right with or without a tunnel
and no local port number ever reaches a config file. The local port is chosen by the OS, so parallel
captures cannot collide. TLS still verifies against the real hostname: the connection sets
`hostaddr` and leaves `host` alone, so `sslmode=verify-full` keeps working through the tunnel.

Install the extra to use it — `pip install 'cumo-db-schema-comparer[ssh]'`. A config that names an
`ssh:` block without it exits 2 saying so, rather than raising an ImportError.

**Host keys are trusted on first use.** An unknown gateway is pinned to `known_hosts` on first
contact; a gateway whose key has *changed* is refused outright and says where to look. That means
the first connection to a new gateway is the unauthenticated one — make it on a network you trust.

As everywhere else here, the config holds a key *path* and a variable *name*. The key never leaves
your disk and the passphrase comes from the environment, or from the keychain in the desktop
application.

The optional desktop application can also store a credential in the OS keychain, for itself only
— see [Desktop application](#desktop-application). The command line reads the environment and
nothing else, whatever is in the keychain.

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

`report.html` is a **single self-contained file**: inline CSS, inline SVG, no font files, no CDN,
nothing fetched at all. It asks for the brand faces by name and falls back to the system stack, so it
carries the same palette and type as the desktop application without adding font files to every
artifact. Upload it as a CI artifact and it opens correctly on a machine with no outbound
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
| `--redact-literals` / `--no-redact-literals` | Mask credential-shaped string literals in definition text and in enum labels. **On by default**; see below. |

## Desktop application

```sh
pip install --pre '.[gui]'     # needs Python 3.12+; --pre because the Slint binding is a beta
cumo-schema-diff-gui
```

Five views, chosen from the rail on the left: **Config** (every parameter of the config file, with
a Check connection button per source), **Run**, **Results**, **Ignores** (the project's rules, with
the bundled defaults shown read-only) and **Credentials**. The rail also carries the open config
file and the button to replace it, so no strip across the top takes height from the views.

Credentials are stored in the OS keychain and resolved **keychain first, environment as fallback**.
A connection string is never shown, never logged and never written to the config file — the UI shows
only where each credential came from and a redacted host/database summary.

**The CLI does not read the keychain.** It reads the environment and nothing else, so a credential
stored here cannot change what a CI run does.

Selecting a row on the Results tab opens a detail pane under the list: the finding's name and how
it compares, then every attribute that differs, with the master and target values side by side, and,
for a printed definition (a view, a routine body, a check expression), a unified diff of the two
texts. A missing or extra object has no attributes to differ, and the pane says so rather than going
blank. Changing the filter or a severity toggle, or starting a new run, clears the pane. The pane
shows the same masked text as the reports.

A cancelled comparison produces no report and says so: the Results banner is only ever green for a
finished run that found nothing. Cancelling stops sources that have not started, but a capture
already connected is abandoned rather than interrupted — PostgreSQL work in flight ends on its own
`statement_timeout`, which at the defaults can be minutes.

### Where it keeps configurations

**The application does not open configuration files.** There is no Open button, a path on the
command line is refused with a message rather than ignored, and nothing is reopened on start-up: it
starts empty every time. You build a configuration in the window and save it.

On start-up it creates `~/.cumo_db_schema_comparer` if it is missing, and **Save** writes
`config.yaml` there. Before this a configuration made in the GUI could not be saved at all, because
saving needed a path nobody had chosen yet.

There is one file and no name to choose. A configuration's `name` becomes the report title and the
JUnit suite name, so it has to be something; the window defaults it rather than asking, because
naming a comparison you cannot reopen adds nothing to it. **Saving again replaces that file.**

`CUMO_SCHEMA_DIFF_HOME` points the folder somewhere else. The test suite sets it, because a test
that reads or writes the home directory of whoever runs it has already failed.

Editing an existing configuration is a job for an editor, or for the command-line tool, which reads
any path you give it.

The application follows the Cubic design system. Sections are cards with an uppercase eyebrow
heading; every form label in every view comes from one shared column, so fields line up when you
move between views; and every list names its columns. On the Results view the findings and the
detail pane sit side by side above 1000px of content width and stack below it. Every colour, size,
radius and spacing step comes from `gui/ui/tokens.slint`, and the HTML report holds the same
palette — a test compares the two
files colour by colour, so a report and the window that produced it cannot disagree about what
"error" looks like. Catalog text — object paths, statuses, attribute names, source states — is set
in IBM Plex Mono; prose is Mulish; the verdict and the detail heading are Archivo.

The three faces ship with the package under the SIL Open Font License, and the application points
Slint at them before the renderer starts. Set `SLINT_FONT_PATH` yourself to use your own licensed
cut instead: an existing value is always left alone. With neither, the UI falls back to the system
faces and the tokens carry the design on their own.

Buttons, text fields and drop-downs keep their native look: Slint's widget set exposes its palette
read-only, so a focus ring and filled accent buttons cannot be set from the markup without replacing
those widgets by hand and losing the keyboard focus and activation they provide. The pill shape the
design system asks for appears where the markup owns the drawing, as the severity badge on each
finding.

The GUI is an optional extra: installing the CLI alone keeps its Python 3.11 floor and its five
dependencies.

The two **Choose…** buttons open a native picker. Slint's binding has none, and a
second GUI toolkit inside its event loop would risk the window, so each dialog runs in a
subprocess — `osascript` on macOS, `zenity` or `kdialog` on a Linux desktop. The dialog opens
wherever the field already points. Windows has no backend: the same approach would work through
PowerShell, but it cannot be exercised from this project's machines or its CI, and a picker that
returns the wrong thing is worse than one that is honestly absent. Where no backend exists the
fields still accept a typed path and the status line says so.

One rough edge remains: the Slint binding is a beta whose Python objects must be freed on the
thread that made them, so the application drives the cyclic garbage collector itself (see
`gui/app.py`). The window also stops repainting while a picker is open, which is the cost of
keeping the dialog out of this process.

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
server version, which schemas, and where the changelog actually lives:

```
$ cumo-schema-diff probe -c config/invoicing.yaml
prod: PostgreSQL 15.19
  database  invoicing as cumo
  encoding  UTF8  collation de_DE.utf8
  schemas   cumo-invoicing, public
  liquibase cumo-invoicing.DATABASECHANGELOG: 214 changeset(s), last tag R7.6.2
```

If `probe` reports `liquibase AMBIGUOUS`, name the right table in the config:

```yaml
    liquibase: { schema: cumo-invoicing, table: DATABASECHANGELOG }
```

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

### Definition text, routine bodies and `--redact-literals`

Definition text is carried in full: the inventory and every report hold a routine's body next to its
hash, so a changed function shows what changed instead of two hashes. Two texts are kept per
definition and they do different jobs. The *canonical* text — the tokens joined by single spaces —
is what equality is decided on, so reformatting a view or reindenting a function body is not drift.
The text the server printed (`pg_get_viewdef`, `prosrc`) is what a diff is drawn from, because it
still has its line breaks: a nine-line view differing in one line renders that line against three
lines of context, not one giant removed line against one giant added line. Both are masked, so the
diff is as safe as the compared value. The HTML report and the GUI's Results tab draw the same diff.
A delta whose text is short and single-line stays a plain before/after row.

Because that text can contain a hardcoded connection string, `compare` and `inventory` mask
credential-shaped string literals (column defaults, view bodies, check expressions, routine
bodies) and enum labels at capture time, before anything is written. A mask keeps a short digest (`'***:3fa9c2d41b07'`), so
two *different* secrets still compare as different and a rotated one still shows as drift.

`--no-redact-literals` shows the real text, for inspecting a database locally. **An inventory or
report produced that way is no longer safe to share or upload as a CI artifact.** Redaction also
changes baseline signatures, so a baseline approved in one mode will not match the other: accepted
findings reappear as new (nothing is hidden). Keep one mode per baseline, normally the default.

The inventory and the report now write schema version 2 and still read version 1, so an existing
capture or `--baseline` file keeps working.

### Reading the exit code in CI

| Code | What to do |
|------|------------|
| 0 | Nothing. |
| 1 | Real drift at or above the gate. Read `report.html`. |
| 2 | Bad config or a missing environment variable. Fix and re-run. |
| 3 | A database could not be inspected. **Retry** — this is usually transient, and it is deliberately not the same as drift. |

## Brand

The logo lives in [`media/`](media/) — an SVG lockup, a light and a dark form, the mark on its own,
and PNGs for anywhere an SVG is not accepted. All of it is generated from `packaging/logo.py`,
which takes its palette from `src/cumo_schema_comparer/gui/ui/tokens.slint`, so the dock icon, the
README header and a slide are the same drawing. `media/README.md` says how to change it.

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

> **`CUMO_SCHEMA_DIFF_TEST_DSN` is destructive.** The suite runs
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
