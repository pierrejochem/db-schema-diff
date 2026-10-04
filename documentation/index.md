---
title: "Compare PostgreSQL schemas across environments"
description: "DB Schema Diff finds schema drift between PostgreSQL environments, structurally and by Liquibase changelog state. Desktop app and CLI for Linux, macOS and Windows, with a CI gate and JSON, JUnit and HTML reports."
---

# DB Schema Diff

**Is QA's schema really the same as production's?** DB Schema Diff answers that before something
fails at runtime. It inventories one *master* PostgreSQL database and compares it against one or more
other environments of the same service, structurally and by Liquibase changelog state.

It ships as a **desktop application** and a **command-line tool**, runs on Linux, macOS and Windows,
and exits with a code your CI pipeline can gate on.

[Install](install.md){ .md-button .md-button--primary }
[Quick start](worked-example.md){ .md-button }
[View on GitHub](https://github.com/pierrejochem/db-schema-diff){ .md-button }

## What it compares

Tables, columns, constraints, indexes, views, materialized views, sequences, functions, procedures,
triggers, enums, domains, composite and range types and extensions — plus Liquibase changelog state:
which environment is behind, by how many changesets, and where the two histories split.

## Why teams use it

- **Real drift only.** Types, defaults, `serial` and identity columns, generated columns, check
  expressions and index expressions are normalised against real PostgreSQL output, so reformatting is
  not reported as a difference.
- **Safe on production.** Every source is inspected in a read-only `REPEATABLE READ` transaction with
  statement and lock timeouts. See [Credentials](credentials.md).
- **Built for CI.** Reports in JSON, JUnit XML and a single self-contained HTML file, and documented
  [exit codes](exit-codes.md) that separate drift from an unreachable database.
- **Credentials stay out of reach.** The command line reads them from the environment only; the
  desktop application keeps the password in the operating system keychain.
- **Adoptable on day one.** A [baseline](worked-example.md) accepts the drift you already have and
  fails only on what is new; [ignore rules](ignores.md) tolerate expected differences without hiding
  anything.
- **Works behind a bastion.** [SSH tunnels](credentials.md#reaching-a-database-through-a-bastion) are
  built in.

## Try it in two minutes

```sh
export PROD_DSN='postgresql://user:pass@db-prod:5432/invoicing'
export QA_DSN='postgresql://user:pass@db-qa:5432/invoicing'

db-schema-diff compare -c config.yaml --html report.html
```

Start from the [example configuration](https://github.com/pierrejochem/db-schema-diff/blob/main/config.example.yaml),
or follow the [worked example](worked-example.md) from first probe to a CI gate.

## Documentation

| | |
|---|---|
| [Install](install.md) | `.deb`, `.rpm`, macOS disk image, Windows build, pip |
| [Command line](cli.md) | Every command and flag, offline comparison, report formats |
| [Credentials](credentials.md) | Environment variables, keychain, SSH tunnels |
| [Liquibase](liquibase.md) | Changelog comparison and how identity is decided |
| [Ignore rules](ignores.md) | Tolerate expected differences, keep them visible |
| [Desktop application](desktop.md) | Connect, run and read results in a window |
| [Exit codes](exit-codes.md) | What each code means and what to do in CI |
| [FAQ](faq.md) | Short answers to common questions |

<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  "name": "DB Schema Diff",
  "applicationCategory": "DeveloperApplication",
  "operatingSystem": "Linux, macOS, Windows",
  "description": "Compare PostgreSQL schemas across environments, structurally and by Liquibase changelog state, from a desktop app or the command line.",
  "url": "https://pierrejochem.github.io/db-schema-diff/",
  "downloadUrl": "https://github.com/pierrejochem/db-schema-diff/releases/latest",
  "codeRepository": "https://github.com/pierrejochem/db-schema-diff",
  "offers": { "@type": "Offer", "price": "0", "priceCurrency": "EUR" }
}
</script>
