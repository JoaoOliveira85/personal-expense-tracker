# Security Policy

## Reporting a vulnerability

Please report security issues privately through GitHub's
[private vulnerability reporting](https://github.com/JoaoOliveira85/personal-expense-tracker/security/advisories/new)
rather than a public issue. Expect an initial response within a week.

## What this project handles

The tracker is a local tool that processes bank statements and, optionally,
mailbox credentials:

- `data/email-config.json` stores the IMAP password in plain text. It is
  created with owner-only permissions and is gitignored. Use an app-specific
  password, never your main one.
- `data/`, `raw/`, `reports/`, `backups/` and the generated reports hold
  financial data. They are gitignored; never commit them.
- The ledger never stores a statement's account number, only a SHA-256
  fingerprint of it salted with a random per-installation value kept in the
  same database. That keeps the number out of the file and its backups in
  readable form; it does not stop someone holding the ledger from guessing a
  short account number by trying them all.
- The GUI binds to `localhost`. The Docker setup listens on `0.0.0.0` inside
  the container, so publish the port only to a trusted network and do not
  expose it to the internet: the GUI has no authentication.
