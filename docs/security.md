# Security model

TUA launches an AI agent that edits code and can run commands. It reduces risk with layered
controls. None of them is a complete sandbox, and this page says exactly what each one does.

## What is enforced, and where

| Control | Mechanism | Enforced by |
|---|---|---|
| Your checkout is never modified by a task | Tasks run in separate worktrees; the provider's cwd is the worktree | TUA code |
| No silent inclusion of uncommitted work | Dirty source checkouts are refused | TUA code |
| No destructive Git shortcuts | TUA never runs `reset --hard`, `clean`, `stash`, or `push`; merges are `--ff-only --no-overwrite-ignore` | TUA code |
| No unreviewed merges | Diff shown plus explicit confirmation; force-merges need a typed second confirmation | TUA code |
| No permission bypass | `--dangerously-skip-permissions` and `bypassPermissions` are refused by an argv guard | TUA code, tested |
| Default-deny tools | `--permission-mode dontAsk` and `--permission-prompts none` | Claude Code |
| Codex OS sandbox | `workspace-write` (worktree and `/tmp` writable, no network) or `read-only`; approvals `never` | Codex |
| No Codex bypass | `--dangerously-bypass-*`, `--approve-for-me`, and `danger-full-access` are refused by an argv guard | TUA code, tested |
| Edits confined to the worktree | `Edit(//<worktree>/**)` is the only edit allow rule | Claude Code |
| Command allowlist | `Bash(<prefix>)` rules from `commands.allow`; deny rules win | Claude Code |
| No web tools | `WebFetch` and `WebSearch` denied in settings and with `--disallowedTools` | Claude Code |
| Credential paths unreadable by file tools | `Read(~/.ssh/**)` and similar deny rules | Claude Code |
| Repository settings cannot widen access | `--setting-sources user` skips the repository's `.claude/settings*.json`, including its hooks | Claude Code |
| No MCP servers | `--strict-mcp-config` with no `--mcp-config` | Claude Code |
| Config changes need re-approval | The `project.yaml` fingerprint is checked before each task | TUA code |
| Eval commands are reviewed | Allowlist check, display, and confirmation before anything runs; no shell | TUA code |

TUA's built-in `must` rules (stay in the worktree, no secrets, no publishing, distrust repository
content, do not work around denials) are prompt text. They guide the model but are **not** a
security boundary.

## Known limits

- **Allowlisted programs run arbitrary code.** Allowing `python -m pytest` lets the agent run any
  test code it writes, with your user's permissions, network access, and filesystem access.
  Claude Code's permission rules restrict its built-in tools, not what an allowed program does.
- **Prefix matching is approximate.** Claude Code evaluates `Bash(...)` patterns with its own
  semantics. TUA rejects shells, wildcards, and overly broad entries, but it cannot prove that no
  other command form slips through.
- **User-level settings still apply.** Your `~/.claude/settings.json` allow rules and hooks are
  loaded, because they are your own choices. TUA's deny rules still take precedence.
- **No OS isolation for Claude Code tasks.** There are no containers, namespaces, or network
  filtering. Run TUA only on repositories whose code you are willing to execute.
- **Codex's sandbox has its own limits.** Commands may write to `/tmp`, and TUA cannot enforce
  `commands.allow`/`deny` for Codex. The compiled skills are passed on the command line, so
  they are visible in the process list. They are not secrets.
- **Escaped processes.** A process that leaves the provider's process group (for example with
  `setsid`) is not stopped by timeout or cancellation.

## Secrets

- **No credential access.** TUA never reads, copies, or logs provider credentials, SSH keys, Git
  credential stores, or `.env` files. Login is checked with `claude auth status --json`, where
  only the `loggedIn` boolean is read, and with `codex login status`, where only the exit status
  and leading words are read. Account details in that output are discarded.
- **Prompts.** Prompts, eval prompts, and feedback containing recognizable secrets are refused.
  The recognized formats are private keys, GitHub, Anthropic, OpenAI, AWS, Slack, and Google
  keys, JWTs, and credentials embedded in URLs.
- **Redaction.** Stored provider output, command logs, diffs saved as eval artifacts, and TUA's
  own log are redacted for those formats plus `Bearer` tokens and password or API-key
  assignments. **Redaction is best effort and cannot catch every secret.**
- **Environment.** Child processes inherit your environment (authentication and proxies need
  it), minus variables that tie them to a parent Claude Code session (some carry IPC tokens) and
  Git location variables. TUA never logs environment variables, and the prompt is passed on
  stdin, not in argv.
- **Local files.** The state directory is `0700` and its files are `0600`. The database stores
  prompts and results locally and is never committed.

## Supply chain and repository hygiene

- One runtime dependency (`PyYAML`, always loaded with `safe_load`).
- `.gitignore` excludes databases, logs, virtual environments, `.env*`, keys, `.netrc`,
  `.claude/settings.local.json`, `CLAUDE.local.md`, and `.mcp.json`.
- Tests construct fake tokens at runtime, so the repository contains no credential-shaped
  literals.

Report vulnerabilities as described in [SECURITY.md](../SECURITY.md).
