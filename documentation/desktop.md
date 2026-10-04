---
title: "Desktop application for comparing PostgreSQL schemas"
description: "The DB Schema Diff desktop application: connect to sources with the OS keychain, watch a comparison run, and read side-by-side diffs of every difference."
---

# Desktop application

```sh
pip install --pre '.[gui]'     # needs Python 3.12+; --pre because the Slint binding is a beta
db-schema-diff-gui
```

Five views, chosen from the rail on the left: **Config** (every parameter of the config file, with
a Check connection button per source), **Run**, **Results**, **Ignores** (the project's rules, with
the bundled defaults shown read-only) and **About**, which says where the configuration is saved.

## Connecting

Each source's card asks for a **host**, **port**, **database**, **user** and **password**, and an
optional **ssl mode** — no environment variable to name, and no connection string to paste. A line
under the fields says what the connection still lacks. Everything but the password is written to the
config file. The password is handed to the OS keychain and is never shown, never logged and never
written to a file; a connection string is assembled from the parts at the moment it is needed, and
it redacts itself everywhere but the connect call.

Changing any of those parts rewrites the stored credential, so the connection a run makes is always
the one the window is showing. Only an entry the application itself stored is rewritten: a DSN you
exported in your environment belongs to you, and the command line is reading that same variable.

**If you already reach a database through your own `ssh -L` forwarding, do not also give the source
an `ssh:` block.** Point `host` and `port` at your local end of the forwarding and leave the gateway
fields empty. The two mechanisms do the same job, and configured together the tool would tunnel to
the gateway and then look for your forwarded port *on the gateway*, which is not where it is.

The variable the credential is filed under is generated from the source's label — `qa` becomes
`DB_QA_DSN` — and written to the config file, so the command-line tool, which resolves `dsn_env`
and nothing else, can run the same configuration once that variable is exported. Renaming a source
renames the variable and moves the stored password with it.

Credentials resolve **keychain first, environment as fallback**, so a DSN already exported still
works and a source backed by one is shown as having a password.

**The CLI does not read the keychain.** It reads the environment and nothing else, so a credential
stored here cannot change what a CI run does.

Without a usable keychain the Config view says so at the top and the Store buttons are disabled,
rather than leaving a button that quietly does nothing.

Selecting a row on the Results tab opens a detail pane under the list: the finding's name and how
it compares, then every attribute that differs, with the master and target values side by side, and,
for a printed definition (a view, a routine body, a check expression), a unified diff of the two
texts. A missing or extra object has no attributes to differ, and the pane says so rather than going
blank. Changing the filter or a severity toggle, or starting a new run, clears the pane. The pane
shows the same masked text as the reports.

## What a comparison is doing

**Start comparison** opens a modal dialog that says what is being compared before it starts and
fills in a checklist as it runs. The top half is read from the configuration — each source with its
host, database, schemas and schema map, the gate, the per-run flags, the ruleset, the baseline — so
a report that comes back clean can be told from a report of the wrong thing. The bottom half is one
row per source and one per catalog step inside it, each showing what it found, followed by the diff
and the report build. Those last two are otherwise invisible: a large comparison looks finished
when the last source is captured, and then sits there.

The first row under each source is the **schemas the server actually matched**, as opposed to the
ones the config asked for. That is the number that explains all the others: a `schemas:` filter
naming a schema the database does not have makes every count below it zero, and the zeros give no
reason for themselves. A source that connects and reads nothing is marked `no objects found`.

Steps that fetch several kinds in one query are broken down: `views` carries `plain views` and
`materialized views` beneath it, `types` carries the enum, domain, composite and range counts. A
matview holds data and has to be refreshed, so it is not the same thing as a view, and a single
`views: 3` could not tell you how many of each you had.

**A comparison that finds nothing on either side is an error, not a pass.** It used to report
"No differences found" and exit 0 — the worst answer this tool can give, because there was no
drift only in the sense that there was nothing to compare. Both sides empty now raises a note at
ERROR, the verdict says so by name, and `compare` exits 1. Run `probe`, or **Check connection**,
to see the schemas each server actually matches.

Cancel is in the dialog, because a modal that hid the only way to stop a long run would be worse
than no dialog. When the run ends the dialog stays, with the verdict and a button to the results —
the log is the point, so it is not thrown away at the moment it becomes worth reading. A source
that never connects reports no steps, and its rows are marked skipped rather than left pending.

Nothing in the dialog is a secret: every line of the inputs block is a value the config file holds,
and a source with no host is described by the name of the variable its credential comes from.

A cancelled comparison produces no report and says so: the Results banner is only ever green for a
finished run that found nothing. Cancelling stops sources that have not started, but a capture
already connected is abandoned rather than interrupted — PostgreSQL work in flight ends on its own
`statement_timeout`, which at the defaults can be minutes.

## Where it keeps configurations

On start-up it creates `~/.db_schema_diff` if it is missing, reads `config.yaml` from it if
that file is there, and **Save** writes it back. One file, always the same path.

**There is still no Open button**, and a path on the command line is refused with a message rather
than ignored — the window would otherwise be showing a different file from the one it says it is.
Another `.yaml` sitting in the folder is not opened either: with no chooser, picking one by name or
by modification time would make the window's contents a guess.

There is no name to choose. A configuration's `name` becomes the report title and the JUnit suite
name, so it has to be something; the window defaults it rather than asking.

A `config.yaml` that cannot be read — bad YAML, or a shape the model rejects — does **not** stop the
window opening. It says what went wrong, starts from an empty configuration, and the next **Save**
replaces the bad file. Refusing to start would leave no way to recover from inside the application.

Hand-editing that file is fine, and the command-line tool reads any path you give it. One caveat:
the window rewrites the file from the configuration rather than patching it, so saving over a
hand-edited file drops its comments. It says so when it loads a file that has any, while they are
still there to copy somewhere else.

`DB_SCHEMA_DIFF_HOME` points the folder somewhere else. The test suite sets it, because a test
that reads or writes the home directory of whoever runs it has already failed.

The application follows one design system. Sections are cards with an uppercase eyebrow
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
