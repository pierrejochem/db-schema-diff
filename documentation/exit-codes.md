---
title: "Exit codes and reading them in CI"
description: "The db-schema-diff exit codes, what each means, and how to react to them in a CI pipeline: drift, bad config, an unreachable database or an internal error."
---

# Exit codes

| Code | Meaning |
|------|---------|
| 0 | In sync at or above the configured `--fail-on` severity |
| 1 | Drift found — the CI gate |
| 2 | Unusable input: bad YAML, missing env var, unsupported server version |
| 3 | A source could not be inspected (connect / auth / permission / timeout) |
| 4 | Internal error — a bug in this tool |
| 130 | Interrupted |

Exit **3 takes precedence over 1**: a partial comparison is never reported as a clean gate.


## Reading the exit code in CI

| Code | What to do |
|------|------------|
| 0 | Nothing. |
| 1 | Real drift at or above the gate. Read `report.html`. |
| 2 | Bad config or a missing environment variable. Fix and re-run. |
| 3 | A database could not be inspected. **Retry** — this is usually transient, and it is deliberately not the same as drift. |
