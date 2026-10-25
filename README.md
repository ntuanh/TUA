# TUA

TUA is a local-first, terminal-first orchestration layer for running coding tasks through an
existing AI coding CLI. Each task is one headless run of
[Claude Code](https://code.claude.com/docs/en/cli-usage) or OpenAI Codex CLI inside its own Git
worktree, started from a recorded commit. Every task gets a durable ID,
fingerprinted skills (instructions), and a SQLite record of its whole lifecycle. You review the
diff and explicitly merge or discard it. Nothing is ever pushed.

> **Status: v0.1 (alpha).** Providers: Claude Code CLI and OpenAI Codex CLI. Linux first. The interfaces may
> change. See [Limitations](#limitations).

## Features

- **Isolated tasks.** Each task runs in a dedicated worktree on a `tua/<task-id>` branch, never in
  your checkout. A dirty checkout is refused, and TUA never stashes, resets, or cleans your work.
- **Reviewed merges.** `tua merge` shows the full diff, asks for confirmation, and fast-forwards
  your branch to one squash commit. It refuses if your checkout is dirty, on another branch, or
  has moved since the task started. Your Git identity is used and nothing is pushed.
- **Versioned skills.** `must` rules and `default` preferences are compiled into a deterministic
  payload. TUA appends it to Claude Code's system prompt and records two SHA-256 fingerprints:
  one for the sources, one for the exact compiled bytes.
- **Fail-closed permissions.** Tasks run with `--permission-mode dontAsk` and generated
  allow/deny rules: edits only inside the worktree, Bash only for allowlisted command prefixes,
  and no web tools. `--dangerously-skip-permissions` is never used.
- **Honest outcomes.** `completed`, `failed`, `timeout`, and `cancelled` are recorded with
  normalized error types. Failed, timed-out, cancelled, and interrupted tasks keep their
  worktree for inspection.
- **Feedback and evals.** `tua good` and `tua bad` attach feedback to a task's exact
  fingerprints. They never edit skills. `tua eval` compares a candidate against a pinned,
  immutable baseline, within run-count and wall-clock budgets.

## Requirements

- Linux (primary target; macOS should mostly work but is untested), Python 3.10+, Git 2.25+.
- Claude Code CLI on `PATH` and logged in (`claude auth login`). TUA was verified with
  Claude Code 2.1.301 and 2.1.310 and checks the exact flags it needs before every task.
- Optional: Codex CLI on `PATH` and logged in (`codex login`), verified with 0.164.0 on a
  ChatGPT plan. Plan-specific models apply; for example, ChatGPT Go worked with `gpt-6-luna`.

## Installation

TUA is not published to PyPI. Install it from a clone:

```bash
git clone https://github.com/ntuanh/TUA.git
cd TUA
python3 -m venv .venv
. .venv/bin/activate
pip install .
tua --version
```

Alternatively, run `pipx install .` from the clone.

## Quick start

The easiest way is to open a terminal in your project folder and type `tua`. If the folder
is not a Git project yet, `tua` offers to make it one safely, or to use a Git project inside
it:

```text
~/code/myapp$ tua
First time here: TUA isn't set up for "myapp" yet.
Set up TUA for this project now? [Y/n]
✓ TUA is ready for "myapp".

What should the AI do? > Add a --verbose flag to the CLI and a test for it
✓ Done in 41s.
[m] merge  [d] diff  [r] review  [x] discard  [Enter] keep for later >
```

The setup questions appear only the first time. After that, `tua` goes straight to the prompt.

The same steps as individual commands, for scripts or more control:

```bash
tua doctor                                 # check git, claude, login, and local state
tua project init ~/code/myrepo --name myrepo
cd ~/code/myrepo && git add .tua && git commit -m "Add TUA config"   # TUA needs a clean checkout
tua run "Add a --verbose flag to the CLI and a test for it" --project myrepo
tua status                                 # list tasks
tua diff <TASK_ID>                         # review the change
tua merge <TASK_ID>                        # shows the diff again, asks, commits locally
tua discard <TASK_ID>                      # or throw it away (asks first)
tua good <TASK_ID> --note "clean change"   # optional feedback
```

`tua project init` creates `.tua/project.yaml` and `.tua/skills/project-defaults.md` if they are
missing. It never overwrites them. Commit them, or add `.tua/` to `.git/info/exclude` to keep
them local.

## Commands

| Command | Purpose |
|---|---|
| `tua` | Interactive mode in the current project: setup on first use, then prompt, run, review, merge |
| `tua --version`, `tua --help` | Version and help (`tua <command> --help` for each command) |
| `tua doctor` | Diagnose Python, Git, Claude Code (version, flags, login), local state, projects |
| `tua project init <PATH> --name <NAME> [--allow CMD ...]` | Create `.tua/project.yaml` if missing (optionally allowing commands such as tests), validate, register |
| `tua project list` / `tua project show <NAME>` | List projects; show config, skills, hashes, permission rules |
| `tua run "<PROMPT>" --project <NAME> [--model M] [--timeout S] [--max-turns N] [--profile read-only]` | Run one task (`--profile` can only narrow the project's permissions) |
| `tua review <TASK_ID> [--provider P] [--model M] [--focus TEXT]` | Have a model review a task's changes, as a recorded read-only task |
| `tua status [TASK_ID] [--project NAME] [--limit N]` | List tasks, or show one with its event log |
| `tua diff <TASK_ID> [--stat]` | Changes against the base commit (works for failed tasks too) |
| `tua merge <TASK_ID> [--yes] [--force] [-m MSG]` | Review and squash-merge locally; never pushes |
| `tua discard <TASK_ID> [--yes]` | Remove the task's worktree and branch after confirmation |
| `tua good <TASK_ID> [--note ...]` / `tua bad <TASK_ID> --reason ... [--note ...]` | Record feedback |
| `tua eval baseline <EVAL_YAML> [--replace]` | Run a suite and store an immutable baseline |
| `tua eval run <EVAL_YAML>` | Run a suite as a candidate and compare it with its pinned baseline |

Both `eval` commands also accept `--max-runs N`, `--max-seconds N`, `--yes`, and
`--keep-worktrees`.

`run` and `review` also accept `--provider claude-code|codex` and `--effort LEVEL`.

`--yes` skips only the confirmation for completed tasks; the diff is still printed. `--force`
lets you merge a failed, timed-out, or cancelled task. It always asks twice, the second time
by typing the task ID, and it never skips safety checks.

Exit codes: `0` success, `1` error, failed task, or incomplete eval, `2` usage error,
`124` task timeout, `130` cancelled.

## Configuration

`.tua/project.yaml` (all paths are relative to the repository root and may not leave it):

```yaml
schema_version: 1
name: myrepo
repository: "."
default_provider: claude-code  # or codex
default_model: sonnet          # optional; passed to `claude --model` / `codex --model`
effort: medium                 # optional; validated per provider
timeout_seconds: 1800
max_turns: 50                  # optional; passed to `claude --max-turns`
permission_profile: safe       # safe | read-only
skills:
  - .tua/skills/project-defaults.md
eval_dir: .tua/evals
commands:
  allow: ["python -m pytest"]  # prefixes the provider's Bash tool and eval suites may run
  deny: ["python -m pip"]
```

A skill file is Markdown with front matter and one `## must: <id>` or `## default: <id>`
section per rule. Global skills are listed in `~/.config/tua/config.yaml`. Editing
`project.yaml` requires re-running `tua project init` before the next task, so a `git pull`
cannot silently widen permissions. Details: [docs/configuration.md](docs/configuration.md).

## Safety model

- Tool permissions come from a generated Claude Code settings file, loaded with
  `--setting-sources user`. Your own user settings still apply. Project and local settings from
  the repository are not loaded, so repository-provided hooks or allow rules cannot widen access.
  MCP servers are disabled with `--strict-mcp-config`.
- **Claude Code permissions are not an OS sandbox.** An allowlisted command such as a test
  runner can run arbitrary code from the repository.
- **Codex tasks use Codex's OS sandbox instead.** `safe` maps to `workspace-write`, which
  allows writes only to the worktree and `/tmp` and blocks the network. `read-only` maps to
  `read-only`. Codex has no per-command allowlist, so `commands.allow` does not apply to it.
  **The Codex sandbox does not restrict reads.** A Codex task can read any file your user can,
  and its conversation goes to the provider. Claude Code tasks cannot read outside the
  worktree. Keep `commands.allow` short and run TUA only on code you
  are willing to execute.
- TUA never reads credential files and reads only the `loggedIn` boolean from
  `claude auth status`. It refuses prompts that contain recognizable secrets, and it redacts
  known secret formats in logs on a best-effort basis.

Full details: [docs/security.md](docs/security.md).

## Local state

TUA keeps everything outside your repositories and respects `XDG_*`:

| What | Where |
|---|---|
| Database, task logs, worktrees, compiled skills, eval artifacts | `~/.local/state/tua/` |
| Global config and global skills | `~/.config/tua/` |
| Temporary files | `~/.cache/tua/` |

Directories are created with mode `0700` and files with mode `0600`.

## Limitations

- Two providers (Claude Code, Codex). Gemini CLI is not supported: personal Google logins
  are currently rejected for it. Tasks are single headless runs, with no conversation follow-up
  inside TUA; `tua status <id>` shows how to resume the session in the provider CLI.
- Codex does not report the model it actually used or a cost; TUA records the requested model
  and leaves the rest unknown.
- Merges only onto an unchanged base. If your branch moved, merge the kept `tua/<id>` branch
  yourself or discard and re-run the task.
- Permission rules use prefix matching and Claude Code's own semantics. They are not a security
  sandbox.
- Rate-limit and authentication errors are classified only from structured fields or distinctive
  messages. Anything else is reported as `provider_error`.
- Eval comparisons over a few LLM runs are noisy, and TUA reports no statistical significance.

## Documentation

- [Architecture](docs/architecture.md)
- [Configuration](docs/configuration.md)
- [Task lifecycle and recovery](docs/task-lifecycle.md)
- [Security](docs/security.md)
- [Evaluations](docs/evaluations.md)
- [Decision log](docs/decisions.md)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Run `scripts/dev-check.sh` before opening a pull request.
Tests use temporary repositories and a fake `claude` executable, so they need no account and
no network. Report security issues as described in [SECURITY.md](SECURITY.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).
