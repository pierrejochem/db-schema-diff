---
title: "Configuration file: output directory and sources"
description: "Where DB Schema Diff writes its reports and how a configuration file names its sources, targets, schemas and output directory."
---

## Where the reports go

A config can name its own output directory, so a service's reports land in the same place on every
machine with no flag to remember:

```yaml
output_dir: reports        # relative to this config file
```

Relative is the form to prefer, for the same reason as `ignores_file`: this file is committed, and
an absolute path names one machine. An absolute path is accepted and used as written. `--out-dir`
overrides it, because a flag is what the person asked for now and the file is what the project
asked for in general.

A config without the field behaves exactly as before — no flag, no files. The desktop application
writes the field when you choose a directory, so the choice survives a restart, and saving the
config to another directory rewrites a relative path to keep pointing where it pointed.
