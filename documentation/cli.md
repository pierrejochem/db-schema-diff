---
title: "Command-line usage: compare PostgreSQL schemas in CI"
description: "Run db-schema-diff from the command line: compare environments, write JSON, JUnit and HTML reports, compare saved inventories offline, and every flag."
---

# Usage

Start from `config.example.yaml`:

```sh
cp config.example.yaml config/invoicing.yaml
db-schema-diff compare -c config/invoicing.yaml
```

`--config` takes a directory too, which runs every service's comparison in one go:

```sh
db-schema-diff compare -c config/
```

## Machine-readable output

```sh
# All three reports into one directory, the convenient form for CI.
db-schema-diff compare -c config/invoicing.yaml --out-dir build/

# Or name them individually.
db-schema-diff compare -c config/invoicing.yaml \
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

## Comparing environments that share no credentials

Production and QA never have to be reachable from the same place at the same time:

```sh
# In the job that can reach production:
db-schema-diff inventory -c config/invoicing.yaml --source prod -o prod.json

# In the job that can reach QA:
db-schema-diff inventory -c config/invoicing.yaml --source qa -o qa.json

# Anywhere, with no credentials in the environment at all:
db-schema-diff diff-inventories prod.json qa.json --junit junit.xml
```

`render` re-renders a saved JSON report in another format, and reproduces its exit code, so a
later pipeline stage can gate on an earlier stage's comparison without repeating it:

```sh
db-schema-diff render --from report.json --junit junit.xml
```


## Useful flags

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
| `--out-dir DIR` | Write all three machine-readable reports into one directory. Overrides the config's `output_dir`. |
| `--sequential` | Capture one source at a time, for debugging. |
| `--redact-literals` / `--no-redact-literals` | Mask credential-shaped string literals in definition text and in enum labels. **On by default**; see below. |
