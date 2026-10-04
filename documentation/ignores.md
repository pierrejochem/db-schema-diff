---
title: "Ignore rules: tolerate expected schema differences"
description: "Quieten expected schema differences without hiding drift: scope rules by object kind, name glob, target, attribute and status, and keep every suppressed finding visible."
---

## Quietening expected differences

Some differences are expected rather than wrong. The bundled ruleset already covers Quartz runtime
state, Liquibase's own tables and conventionally-named scratch objects; add your own on top:

```sh
db-schema-diff compare -c config/invoicing.yaml --ignores ignores.yaml
```

Start from `ignores.example.yaml`. A rule can be scoped by object kind, name glob, target label,
attribute and status, and `statuses: [extra_in_target]` is what lets a rule tolerate an extra object
without also tolerating a missing one.

Nothing is hidden without trace. A suppressed finding still appears in the JSON report and as a
skipped case in JUnit, tagged with the rule that suppressed it, and the console says how many were
set aside:

```sh
db-schema-diff compare -c config/invoicing.yaml --show-ignored      # list them
db-schema-diff compare -c config/invoicing.yaml --no-default-ignores # start from nothing
```

Prefer `action: warn` over `ignore` while something is still being worked on: the finding stays
visible and stops failing an `--fail-on error` gate.

### Rule keys

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
