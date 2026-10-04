---
title: "Credentials and SSH tunnels for PostgreSQL schema comparison"
description: "How DB Schema Diff handles database credentials safely: environment variables only, a read-only transaction, the OS keychain in the desktop app, and SSH bastion tunnels."
---

# Credentials

For the command line, credentials **only** come from the environment. The YAML config names targets
and points at environment variable *names*; it never contains a DSN. There is no `--dsn` and no
`--password` flag, by design, so argv and shell history cannot carry a credential.

```sh
export PROD_INVOICING_DSN='postgresql://user:pass@db-prod:5432/invoicing'
export QA_INVOICING_DSN='postgresql://user:pass@db-qa:5432/invoicing'
```

The desktop application asks for the parts instead — host, port, database, user, password — builds
the connection string itself and puts it in the OS keychain under a variable name it generates from
the source's label. The parts go in the config file, which stays non-secret and committable; the
password goes to the keychain and nowhere else. The generated name is in the file too, so a config
written there still runs unchanged on the command line once that variable is exported in CI. See
[Desktop application](desktop.md).

Every source is inspected in a read-only `REPEATABLE READ` transaction with
`statement_timeout` and `lock_timeout` set, so pointing this at production is safe.

## Reaching a database through a bastion

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

Install the extra to use it — `pip install 'db-schema-diff[ssh]'`. A config that names an
`ssh:` block without it exits 2 saying so, rather than raising an ImportError.

**Host keys are trusted on first use.** An unknown gateway is pinned to `known_hosts` on first
contact; a gateway whose key has *changed* is refused outright and says where to look. That means
the first connection to a new gateway is the unauthenticated one — make it on a network you trust.

As everywhere else here, the config holds a key *path* and a variable *name*. The key never leaves
your disk and the passphrase comes from the environment, or from the keychain in the desktop
application.

The command line reads the environment and nothing else, whatever is in the keychain.
