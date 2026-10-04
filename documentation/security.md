---
title: "Redaction, read-only inspection and safe sharing of reports"
description: "How DB Schema Diff keeps secrets out of its reports: read-only transactions, masked credential-shaped literals, and what to know before sharing an inventory."
---

## Definition text, routine bodies and `--redact-literals`

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
