# Security policy

## Supported versions

TUA is pre-1.0. Only the latest commit on `main` receives security fixes.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's **"Report a vulnerability"** button
(Security tab → Advisories) on this repository. Do not open a public issue. Include the TUA
version, your Claude Code version (`claude --version`), and steps to reproduce.

Never include real credentials, tokens, or private repository content in a report. If a secret
was exposed, revoke or rotate it first.

## Scope

The following are in scope:

- bypasses of TUA's permission plan (`--permission-mode dontAsk` and the generated rules)
- edits outside a task worktree
- user changes lost or overwritten during `run`, `merge`, or `discard`
- secrets leaking into logs or the database
- eval commands executed without validation or confirmation

TUA's permission rules are not an OS sandbox. Code run by an explicitly allowlisted command is
expected behaviour; see [docs/security.md](docs/security.md) for the documented limits.
