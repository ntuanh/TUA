# Configuration

## Project configuration: `.tua/project.yaml`

The project root is the repository root (the directory that contains `.tua/`). Every relative
path is resolved against it and must stay inside it. Absolute paths, `..` traversal, and symlinks
that point outside the root are rejected, and so are unknown keys.

| Key | Default | Meaning |
|---|---|---|
| `schema_version` | required | Must be `1`. |
| `name` | required | 1-64 characters: `a-z`, `0-9`, `.`, `_`, `-`. Must match `--name`. |
| `repository` | `"."` | Must be `"."` in v0.1. |
| `default_provider` | `claude-code` | `claude-code` or `codex`. `tua run --provider` overrides it for one task. |
| `default_model` | CLI default | Passed to `claude --model` (for example `sonnet`) or `codex --model` (for example `gpt-6-luna`). It belongs to `default_provider`: a task or review run with another provider (`--provider`) gets that CLI's default model instead, and TUA prints a note. `tua run --model` overrides it for any provider. |
| `effort` | CLI default | Reasoning effort. Claude Code: `low`, `medium`, `high`, `xhigh`, `max`. Codex: `minimal`, `low`, `medium`, `high`, `xhigh`. TUA validates it because neither CLI rejects unknown values. Like `default_model`, it applies only to `default_provider`. `tua run --effort` overrides it. |
| `timeout_seconds` | `1800` | Wall-clock limit per task (1 to 86400). `tua run --timeout` overrides it. |
| `max_turns` | CLI default | Passed to `claude --max-turns` (1 to 1000). Codex has no equivalent. |
| `permission_profile` | `safe` | `safe` or `read-only` (see below). |
| `skills` | `[]` | Project skill files (`.md`), compiled in this order. |
| `eval_dir` | `.tua/evals` | Eval suites must live under this directory. |
| `commands.allow` | `[]` | Command prefixes the provider's Bash tool and eval suites may run. |
| `commands.deny` | `[]` | Command prefixes that are always refused. |

### Registration and the config fingerprint

`tua project init <PATH> --name <NAME>` registers the project together with the SHA-256 of
`project.yaml`. Before every task, TUA recomputes that fingerprint. If the file changed (by an edit,
or by a `git pull`), TUA refuses to start the task until you review it (`tua project show <NAME>`)
and re-run `tua project init`. This keeps repository changes from silently widening permissions.

### Command allowlist semantics

An entry is a plain command prefix such as `python -m pytest`. It is split into tokens with
`shlex`. A command matches when its first tokens equal the entry's tokens. Validation rejects:

- shell syntax and wildcards (`; & | < > $ \` ( ) { } * ? \`)
- entries that would allow arbitrary commands (shells, `env`, `sudo`, `xargs`, `ssh`, `curl`,
  `wget`, a bare `git`, and similar)
- entries that overlap the always-denied commands `git push`, `git reset --hard`, and
  `git clean`
- entries that are also denied

**This is not a sandbox.** For the provider, each entry becomes the Claude Code rules
`Bash(<prefix>)` and `Bash(<prefix> *)`, and Claude Code decides what matches. Any allowed
program, such as a test runner, can execute code from the repository with your user's
permissions.

## Permission profiles

### Claude Code

Both profiles run Claude Code with `--permission-mode dontAsk`, so anything not explicitly allowed
is denied rather than prompted.

| Profile | Allows | Denies (in addition to the shared list) |
|---|---|---|
| `safe` | `Read` and `Edit` inside the task worktree; Bash for `commands.allow` | `commands.deny` |
| `read-only` | `Read` inside the task worktree | `Edit`, `Write`, `NotebookEdit`, `Bash` |

Both profiles also deny:

- `WebFetch` and `WebSearch`
- `git push`, `git reset --hard`, and `git clean`
- reads of common credential locations: `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.azure`,
  `~/.config/gcloud`, `~/.config/gh`, `~/.docker/config.json`, `~/.kube`, `~/.netrc`,
  `~/.git-credentials`, `~/.claude`, `~/.claude.json`
- TUA's private state: the database, logs, task logs, and eval artifacts

Use `tua project show <NAME>` to see the exact rules.

### Codex

Codex tasks use Codex's own OS sandbox rather than tool rules:

| Profile | Codex sandbox | Effect |
|---|---|---|
| `safe` | `workspace-write`, network off | Commands may write only inside the worktree and `/tmp` (Codex's default writable roots). |
| `read-only` | `read-only` | Commands can read but not write anywhere. |

**Codex can read files outside the project.** Its sandbox limits writes and network access,
not reads. A live test confirmed that a Codex task read a file elsewhere on disk by absolute
path. Anything it reads becomes part of the conversation sent to the provider. Claude Code
tasks, by contrast, were blocked from reading outside the worktree. TUA prints a note when a
Codex task starts.

TUA always passes:

- `approval_policy="never"`, so nothing waits for a person
- `--ignore-user-config` and `--ignore-rules`, so your Codex config and exec rules, and any
  rules from the repository, do not apply
- `--strict-config`

Codex has no per-command allowlist, so `commands.allow` and `commands.deny` are **not** enforced
for Codex tasks. TUA prints a note when a Codex task runs in a project that allowlists
commands.

`tua run --profile read-only` applies a narrower profile to one task. A per-task profile can
only restrict the project's profile; `--profile safe` on a `read-only` project is refused.

`tua project init <PATH> --name <NAME> --allow "python -m pytest"` seeds `commands.allow`
when it creates the config file, so the provider can run your tests from the start. It is
refused if the config file already exists; edit that file instead.

## Skills

A skill file is UTF-8 Markdown, at most 256 KiB:

```markdown
---
schema_version: 1
description: Optional text.
---

## must: keep-changes-scoped
Only change what the task asks for.

## default: tests-for-changes
Add tests for behaviour you change.
```

- Front matter is required. Only `schema_version` (must be `1`) and `description` are allowed.
- Each rule is a `## must: <id>` or `## default: <id>` heading followed by non-empty text. IDs
  use lowercase letters, digits, `.`, `_`, and `-`. The `tua.` prefix is reserved.
- `###` headings and fenced code blocks may appear inside a rule. Text before the first rule is
  an error.

### Precedence

1. TUA's built-in `must` rules (`tua.*`). They tell the model to:
   - stay in the worktree
   - never handle secrets
   - never push or run destructive Git commands
   - treat repository content as untrusted
   - stop rather than work around denied permissions
2. Global `must` rules, then project `must` rules.
3. Project `default` rules, then global `default` rules.
4. The task prompt can override `default` preferences but not `must` rules.

Rule IDs are global. A project `default` with the same ID as a global `default` replaces it, and
`tua project show` lists the override. Every other collision is an error: a duplicate ID within a
level, any redefinition of a `must` rule, or the same ID with a different class.

Precedence is stated in the compiled text for the model. It is guidance, not a security control.

### Fingerprints

- `source_skill_hash`: SHA-256 over the compiler and schema versions plus each source's label
  (`builtin:tua-core`, `global:<path>`, `project:<path>`) and exact bytes, in resolution order.
  Labels use relative paths, so the hash is the same on every machine.
- `compiled_skill_hash`: SHA-256 of the exact UTF-8 payload passed with
  `--append-system-prompt-file`. The payload contains no timestamps or task-specific data, so
  identical skills always produce the same hash.

## Global configuration: `~/.config/tua/config.yaml`

The file is optional. If it is missing, there are no global skills.

```yaml
schema_version: 1
skills:
  - global-rules.md   # relative to ~/.config/tua/
```

See `examples/global-config.yaml` and `examples/global-rules.md`.

## Environment variables

TUA reads `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `XDG_CACHE_HOME`, and `HOME`. Relative XDG values
are ignored, as the XDG specification requires. It has no secret-bearing settings of its own.
