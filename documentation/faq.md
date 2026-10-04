---
title: "FAQ: comparing PostgreSQL schemas and detecting schema drift"
description: "Answers to common questions about DB Schema Diff: how to compare two PostgreSQL databases, detect Liquibase drift, supported server versions, safety on production and CI use."
---

# Frequently asked questions

## How do I compare two PostgreSQL schemas?

Point a configuration at a master database and one or more targets, export each connection string
as an environment variable, and run `db-schema-diff compare -c config.yaml`. The result is a console
summary plus optional JSON, JUnit and HTML reports. See the [worked example](worked-example.md).

## What counts as schema drift?

Any structural difference between environments: a missing or extra table, column, constraint,
index, view, sequence, routine, trigger or type, or a difference in one of their attributes. Cosmetic
differences such as reformatted view text or a reindented function body are normalised away and are
not reported.

## Does it compare Liquibase changelogs?

Yes, automatically. It reports which environment is behind, by how many changesets, and where the
two histories diverge. See [Liquibase](liquibase.md).

## Which PostgreSQL versions are supported?

PostgreSQL 11 and newer. An older server is refused with exit code 2. The test suite runs against
11, 13, 15 and 17.

## Is it safe to point at production?

Every source is inspected in a read-only `REPEATABLE READ` transaction with `statement_timeout` and
`lock_timeout` set. Credentials are never accepted as command-line flags, so argv and shell history
cannot carry one. See [Credentials](credentials.md).

## Can it run in CI?

Yes. It writes JUnit XML that GitHub Actions and most CI systems show as test failures, and its
[exit codes](exit-codes.md) separate real drift (1) from an unreachable database (3), so a transient
network failure is not mistaken for drift.

## How do I adopt it on databases that have already drifted?

Run once and keep the report as a baseline, then pass `--baseline`. From then on the run fails only
on findings the baseline does not contain, and nothing is hidden. See the
[worked example](worked-example.md).

## Can two environments be compared without sharing credentials?

Yes. Capture each environment separately with `db-schema-diff inventory`, then compare the saved
files anywhere with `db-schema-diff diff-inventories`. See [Command line](cli.md).

## Does it work through an SSH bastion?

Yes. Add an `ssh:` block to a source and the tunnel is opened for exactly as long as the connection
needs it. See [Credentials](credentials.md#reaching-a-database-through-a-bastion).

## Which operating systems are supported?

Linux (`.deb` and `.rpm`, amd64 and arm64), macOS (one universal app for Intel and Apple silicon) and
Windows x64. See [Install](install.md).

## Will secrets in view or function definitions leak into reports?

Credential-shaped string literals are masked at capture time, before anything is written. See
[Redaction and safe sharing](security.md).

<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "FAQPage",
  "mainEntity": [
    {"@type": "Question", "name": "How do I compare two PostgreSQL schemas?",
     "acceptedAnswer": {"@type": "Answer", "text": "Point a configuration at a master database and one or more targets, export each connection string as an environment variable, and run db-schema-diff compare -c config.yaml. The result is a console summary plus optional JSON, JUnit and HTML reports."}},
    {"@type": "Question", "name": "Does DB Schema Diff compare Liquibase changelogs?",
     "acceptedAnswer": {"@type": "Answer", "text": "Yes, automatically. It reports which environment is behind, by how many changesets, and where the two histories diverge."}},
    {"@type": "Question", "name": "Which PostgreSQL versions are supported?",
     "acceptedAnswer": {"@type": "Answer", "text": "PostgreSQL 11 and newer. An older server is refused with exit code 2. The test suite runs against 11, 13, 15 and 17."}},
    {"@type": "Question", "name": "Is it safe to point DB Schema Diff at production?",
     "acceptedAnswer": {"@type": "Answer", "text": "Every source is inspected in a read-only REPEATABLE READ transaction with statement_timeout and lock_timeout set, and credentials are never accepted as command-line flags."}},
    {"@type": "Question", "name": "Can DB Schema Diff run in CI?",
     "acceptedAnswer": {"@type": "Answer", "text": "Yes. It writes JUnit XML and its exit codes separate real drift (1) from an unreachable database (3)."}},
    {"@type": "Question", "name": "Which operating systems are supported?",
     "acceptedAnswer": {"@type": "Answer", "text": "Linux (.deb and .rpm, amd64 and arm64), macOS (universal app for Intel and Apple silicon) and Windows x64."}}
  ]
}
</script>
