# TUA

**Run Claude Code or Codex on a safe copy of your project, see exactly what changed, then keep
it or throw it away.**

Type `tua` in any folder, describe what you want, and the AI works on a separate copy of your
project (a Git worktree), never on your files. When it finishes, you review the diff and decide:
merge it as one local commit, ask another model to review it, or discard it. Every task gets an
ID and a full record of what ran, with which model, rules, and permissions. Nothing is ever
pushed for you.

```text
~/code/myapp$ tua

TUA · myapp · claude-code (sonnet)
Web access: off (type /web to turn it on).
Describe what you want, like you would to a colleague: what, where, and how to
check it. End a line with \ to continue it; type @file to send a file's text.

What should the AI do? (Enter on an empty line to quit)
> Add a --verbose flag to the CLI and a test for it

✓ Done in 41s.
Added a --verbose flag to src/app/cli.py and tests/test_cli.py::test_verbose; all tests pass.
Changes: 2 files changed, 38 insertions(+), 3 deletions(-)

[m] merge  [d] diff  [r] review  [x] discard  [Enter] keep for later >
```

> **Status: v0.1 (alpha).** Providers: Claude Code CLI and OpenAI Codex CLI. Linux first.
> Interfaces may change. See [Limitations](#limitations).

## Contents

- [Features](#features)
- [How it works](#how-it-works)
- [Architecture](#architecture)
- [User guide](#user-guide)
- [Command reference](#command-reference)
- [Configuration](#configuration)
- [Safety model](#safety-model)
- [Local state](#local-state)
- [Limitations](#limitations)
- [Documentation](#documentation) · [Contributing](#contributing) · [License](#license)

## Features

### Works like `claude`, with an undo button

- **One command.** `tua` starts an interactive session in the current folder. On first use it
  asks a few yes/no questions, then goes straight to the prompt every time after.
- **Any folder.** Inside a Git project, TUA uses it. In a folder that *contains* a project, the
  AI edits the project and can also read, but not change, the surrounding folder. In a
  subfolder, it tells the AI to focus there. In a plain folder, it offers to make it a Git
  project, leaving out large files, secrets, and nested projects.
- **Review before anything lands.** After each task you see the change summary and choose
  `[m]` merge, `[d]` diff, `[r]` review, `[x]` discard, or keep it for later.

### Safe by design

- **Isolated copies.** Each task runs in its own worktree on a `tua/<task-id>` branch, started
  from your last commit. Your checkout is never the AI's working directory.
- **No surprises in your checkout.** A dirty checkout is refused. TUA never stashes, resets,
  cleans, or overwrites your work. It offers to commit or hide leftovers only when you agree.
- **Careful merges.** `tua merge` shows the full diff, asks, and fast-forwards your branch to
  one squash commit made with your own Git identity. It refuses if your branch moved, is dirty,
  or is a different branch. Nothing is pushed.
- **Fail-closed permissions.** Claude Code runs with `--permission-mode dontAsk` and generated
  allow/deny rules: edits only inside the copy, shell commands only from your allowlist, no MCP
  servers. Codex runs in its OS sandbox with the network off for commands.
  `--dangerously-skip-permissions` is never used.
- **Web access only when you ask.** Web search and page fetching are off by default. Turn them on
  for one task (`tua run --web`), for a session (type `/web` in `tua`), or answer yes when `tua`
  notices a request that mentions the internet. Each task records whether it had web access.
- **Secrets stay out.** TUA never reads credential files, refuses prompts that contain
  recognizable secrets, never offers secret-like files for commit, and redacts logs on a
  best-effort basis.

### Several models, one workflow

- **Claude Code** (Haiku, Sonnet, Opus) and **OpenAI Codex** (models your ChatGPT plan
  includes), chosen per project or per task with `--provider`, `--model`, and `--effort`.
- **Second opinions.** `tua review <ID> --model sonnet` (or `--provider codex`) has another model
  review a change as a recorded, read-only task.
- **Read-only tasks.** `--profile read-only` lets the AI read and explain code without editing
  anything or running commands.
- **Research with sources.** With web access on, the AI can look up documentation, papers, and
  repositories and combine them with your code and the folder around it.

### Reproducible and measurable

- **Durable task records.** SQLite stores every task's state machine, events, prompt, base
  commit, model, CLI version, usage, and permission denials.
- **Fingerprinted rules.** Project and global *skills* (`must` rules and `default` preferences)
  are compiled into one deterministic payload, recorded with SHA-256 hashes of the sources and
  of the exact bytes sent.
- **Feedback and evals.** `tua good` / `tua bad` attach feedback to a task's exact
  fingerprints. `tua eval` compares a candidate setup against a pinned, immutable baseline,
  within run and time budgets.

## How it works

### One task, start to finish

```text
 your project (last commit)
        │
        │ 1. git worktree add -b tua/<id>
        ▼
 ┌──────────────────────────────┐     2. AI CLI runs headless here:
 │  copy: ~/.local/state/tua/   │◄──── claude --print …  or  codex exec …
 │        worktrees/<id>        │      with TUA's rules and permissions
 └──────────────┬───────────────┘
                │ 3. TUA records the result (status, model, usage, log)
                ▼
          you choose:
   [d] diff ── shows every change against the base commit
   [r] review ─ another model reads the change (read-only task)
   [m] merge ── one squash commit on your branch (local only)
   [x] discard ─ copy and branch deleted, your files untouched
```

In more detail, `tua` (or `tua run`) does this for every task:

1. **Finds the project** from the folder you are in and loads its registered settings. If
   `.tua/project.yaml` changed since you approved it, TUA shows the new permissions and asks
   again, so a `git pull` cannot quietly widen what the AI may do.
2. **Checks your checkout** is clean and on a branch, and records the branch and commit.
3. **Creates the task** in SQLite with a new ID (`created` → `preparing`).
4. **Compiles the skills** (built-in TUA rules, your global rules, the project's rules) into one
   payload and hashes it. It **generates the permission rules** for this task's copy.
5. **Preflights the provider at no cost.** It runs the exact command with empty input to prove
   every flag is supported, then checks you are logged in. It reads only a yes/no login flag,
   never a credential.
6. **Creates the copy** with `git worktree add` and verifies it starts at the recorded commit.
7. **Runs the AI CLI headless** in the copy, with the prompt on stdin, inside its own process
   group with a timeout. Ctrl+C stops it cleanly. Short `[TUA]` notes before your request tell
   the AI where you started, whether web access is on, and how to list files.
8. **Records the outcome** (`completed`, `failed`, `timeout`, or `cancelled`) with a normalized
   error type, the model actually reported, turns, usage, and any denied tool calls.
9. **Waits for your decision.** A merge snapshots the copy, shows the diff, checks again that
   nothing moved, commits with your identity, and fast-forwards your branch. A discard removes
   the copy and its branch after confirmation. Failed tasks keep their copy so you can inspect
   them.

### Why Git?

Git is what makes the copy, the exact change list, and the undo possible. Plain `claude` edits
your files directly, while TUA gives the AI a disposable copy and only touches your branch when
you say `m`. If a folder is not a Git project yet, `tua` shows exactly what it would save and
asks before creating one. It never does this in your home folder.

### What `tua` does in each kind of folder

| You start `tua` in… | What happens |
|---|---|
| a Git project (or anywhere inside one) | uses it; from a subfolder, the AI is told to focus there |
| a folder that *contains* Git projects (e.g. `~/dev/FX1` with `fx1/`) | asks which one to use (only once). The AI edits a copy of that project and can **read** the rest of the folder (docs, reports), never change it. |
| a folder with no Git | shows what would be committed and asks "Make this folder a Git project now? [Y/n]". Files over 50 MB, secret-like files (`.env`, keys), and nested projects are left out and added to `.gitignore`. |
| a project with uncommitted or leftover files | lists them and offers `[c]` commit or `[i]` hide them from Git on this machine only. Hiding never deletes anything; secrets and files over 50 MB are never offered for commit. |

## Architecture

TUA is a thin, synchronous Python CLI. There is no server, daemon, or background process: each
`tua` command does its work and exits. One task is one headless run of an AI coding CLI.

```text
                 tua  (cli.py · interactive.py)
                  │
   ┌──────────────┼──────────────────────────┬───────────────────────┐
   ▼              ▼                          ▼                       ▼
 project setup   task service (tasks.py)    review (review.py)      feedback / eval / doctor
 projects.py      ├─ skill compiler           diff · merge · discard   feedback.py
 config.py        │   skills.py → payload     peer review              evaluations.py
 gitsetup.py      │   + SHA-256 hashes        (peer_review.py)         doctor.py
                  ├─ permission builder
                  │   permissions.py
                  ├─ worktree manager
                  │   worktrees.py · repository.py (the only way TUA runs git)
                  └─ provider adapters
                      adapters/claude_code.py ──► claude --print
                      adapters/codex.py ───────► codex exec --json
                          └─ process runner (process.py): process group, timeout, cancel

            all state ──► SQLite (db.py) + private files under ~/.local/state/tua
```

| Layer | Modules | Responsibility |
|---|---|---|
| Interface | `cli.py`, `interactive.py` | Commands, the `tua` session, rendering, confirmations. No SQL. |
| Projects | `config.py`, `projects.py`, `gitsetup.py` | Strict config parsing, registration with a config fingerprint, safe Git setup for plain folders. |
| Tasks | `tasks.py`, `models.py` | Task creation, preparation, execution, recording, crash reconciliation; the state machine. |
| Git | `repository.py`, `worktrees.py`, `review.py` | Git with argument lists and a scrubbed environment; worktrees, temporary-index snapshots, diff, merge, discard. |
| Rules | `skills.py`, `permissions.py` | Skill precedence and deterministic compilation; permission profiles to provider rules. |
| Providers | `adapters/base.py`, `adapters/claude_code.py`, `adapters/codex.py`, `process.py` | One contract (`AdapterRequest` → `AdapterResult`): detect, preflight, login check, run, normalize. |
| Quality | `peer_review.py`, `feedback.py`, `evaluations.py` | Model reviews, append-only feedback, baseline-vs-candidate evals. |
| Storage | `db.py`, `paths.py`, `logging_utils.py` | SQLite migrations and transactions, private XDG paths, redacted logs. |

**Task states**

```text
created → preparing → running → completed | failed | timeout | cancelled
completed → merged | discarded
failed | timeout | cancelled → discarded | merged (--force, confirmed twice)
```

Every transition is checked against this table and written together with its event in one
SQLite transaction. Task identity columns are immutable and the event and feedback tables are
append-only, both enforced by triggers.

**Adding a provider** means implementing the four adapter methods (`detect`, `check_ready`,
`login_state`, `run`). Anything a CLI does not report reliably is stored as unknown, not guessed.

More: [docs/architecture.md](docs/architecture.md),
[docs/task-lifecycle.md](docs/task-lifecycle.md), [docs/decisions.md](docs/decisions.md).

## User guide

### 1. Install (once per machine)

Requirements: Linux (macOS should mostly work but is untested), Python 3.10+, Git 2.25+.

```bash
git clone https://github.com/ntuanh/TUA.git ~/solo/TUA
pipx install --editable ~/solo/TUA     # `tua` now works in every folder and terminal
tua --version                          # tua 0.1.0
```

No pipx? Use a virtual environment: `python3 -m venv .venv && . .venv/bin/activate && pip install .`
If you see `tua: command not found`, run `pipx ensurepath` and open a new terminal.

### 2. Log in to an AI CLI (once)

TUA uses the AI CLIs you already have, with your existing subscription. It never asks for or
stores a key.

| Provider | Install and log in | Notes |
|---|---|---|
| Claude Code (required) | `npm install -g @anthropic-ai/claude-code`, then `claude auth login` | Works with a Claude Pro plan. Verified with Claude Code 2.1.301 and 2.1.310. |
| Codex (optional) | `npm install -g @openai/codex`, then `codex login` | Works with ChatGPT plans that include Codex models; on ChatGPT Go, `gpt-6-luna` worked. Verified with 0.164.0. |

Then check everything:

```bash
tua doctor      # Python, Git, Claude Code (version, flags, login), Codex, local state
```

### 3. First run in a project

```bash
cd ~/code/myapp
tua
```

The first time, TUA asks a few questions. Press Enter to accept the suggested answer:

1. `Set up TUA for this project now? [Y/n]`
2. `Let the AI run "python -m pytest" to test its own work? [Y/n]` (shown when TUA recognizes
   pytest, `npm test`, `cargo test`, or `go test`)
3. `Save TUA's settings in Git (commit .tua/)? [y/N]`. The default keeps them on this machine
   only.

If the repository already ships TUA settings, TUA shows their permissions and asks whether you
trust them before using them.

### 4. Every run after that

`tua` goes straight to the prompt. Type a request and press Enter. When the AI finishes, you
see its full answer (also saved as `result.md` in the task folder) and a change summary. Then
choose:

| Key | Action |
|---|---|
| `m` | **merge**: keep the change as one local commit (nothing is pushed) |
| `d` | **diff**: show the full change |
| `r` | **review**: ask another model to check the change |
| `x` | **discard**: delete the copy; your files are untouched |
| Enter | **keep for later**: decide later with `tua merge <ID>` or `tua discard <ID>` |

Press Enter on an empty prompt (or Ctrl+D) to quit. When you are happy, push as usual:
`git push`.

### 5. Writing good prompts

Describe the task like you would to a colleague: **what** to change, **where**, and **how to
check it**.

```text
> Add a --verbose flag to the CLI in src/app/cli.py that prints each file it processes.
  Add a test in tests/test_cli.py and run the tests.

> The date parser in utils/dates.py fails on "2026-02-30". Make it raise ValueError
  with a clear message, and add a test for it.

> Explain how the upload flow works, from the API route to the storage layer.
  Don't change anything.
```

- End a line with `\` to continue on the next line.
- For a long request, put it in a file and type `@path/to/request.md`.
- The AI can run your tests only if you allowed the command (step 3, or
  [Let the AI run commands](#7-let-the-ai-run-commands)).

**Research on the internet.** Web access is off unless you turn it on:

```text
> /web
Web access is on for this session: the AI can search the web and
open pages. Web pages can try to mislead the AI, ... Type /web again to turn it off.

> Summarize the docs and source in this folder, then search the web for public ISP
  (image signal processing) pipeline designs. Explain the order and purpose of each
  block (black level, demosaic, 3A, white balance, ...) for a solution architect.
```

If you forget, `tua` notices words like "internet", "online", or a URL and asks
`Let the AI search the web for this request? [y/N]`. From a script, use `tua run ... --web`.
Only turn it on when you need it: web pages can contain instructions that try to mislead the
AI, and anything it read in your project could leave in a web request.

### 6. Choosing a model and provider

```bash
tua run "Fix the failing date parser" --project myapp --model haiku      # fast, light on quota
tua run "Refactor the storage layer" --project myapp --model opus        # hardest tasks
tua run "Fix the failing date parser" --project myapp \
    --provider codex --model gpt-6-luna --effort medium                  # ChatGPT (Codex)
tua run "Explain the auth flow" --project myapp --profile read-only      # no edits, no commands
```

Haiku is a good default. Sonnet is a good choice for reviews and harder fixes. To change a
project's default, edit `.tua/project.yaml` (`default_provider`, `default_model`, `effort`) and
run `tua` or `tua project init` again to approve it.

The **cost** TUA shows is the CLI's API-equivalent estimate. On a subscription plan you are not
billed per token; tasks use your plan's quota.

### 7. Let the AI run commands

By default the AI can read and edit files in its copy but cannot run shell commands. Allow the
ones you trust, such as tests:

```yaml
# .tua/project.yaml
commands:
  allow: ["python -m pytest", "npm test"]
  deny: []
```

Then run `tua` (or `tua project init <PATH> --name <NAME>`) to approve the change. An allowed
test command can run any code in your project, so keep the list short. Codex does not use this
list; its sandbox lets it run commands but write only inside its copy and `/tmp`.

### 8. Ask a second model to review

From the menu press `r`, or:

```bash
tua review <TASK_ID> --model sonnet
tua review <TASK_ID> --provider codex --model gpt-6-luna --focus "input validation"
```

The review is its own read-only task with its own ID, linked to the reviewed task
(`tua status <TASK_ID>` lists it). Treat it as an extra opinion: confirm findings with tests.

### 9. Scripting and several projects

Every interactive step has a command, which is handy for scripts and for working on several
projects from one terminal:

```bash
tua project init ~/code/api --name api --allow "python -m pytest"
tua project list
tua run "Add pagination to GET /items" --project api
tua status                      # list tasks and IDs
tua diff <TASK_ID>              # the change
tua merge <TASK_ID>             # shows the diff, asks, commits locally
tua discard <TASK_ID>           # or throw it away (asks first)
tua good <TASK_ID> --note "clean change"
```

`tua run` needs a clean checkout: commit `.tua/` or list it in `.git/info/exclude` first.

### 10. Common messages

| Message | What to do |
|---|---|
| `has uncommitted or untracked changes` | In `tua`, choose `[c]` commit or `[i]` hide. With `tua run`, commit your work first. |
| `has no commits yet` | Make a first commit, or run plain `tua` and let it set Git up. |
| `is not inside a Git repository` | Run plain `tua` there; it offers to set Git up for you. |
| `changed since project ... was registered` | Run `tua` (or `tua project init`) again and approve the new settings. |
| Task status `rate_limited` | You reached your plan's limit. Wait for it to reset. |
| Task status `failed` or `timeout` | `tua status <ID>` shows the reason and how to resume the session; `tua diff <ID>` shows partial work. |
| `has moved from the task's base` when merging | You committed after the task started. Discard it and run the task again, or merge the kept `tua/<ID>` branch yourself. |

The full guide, with more examples: [docs/user-guide.md](docs/user-guide.md).

## Command reference

| Command | Purpose |
|---|---|
| `tua` | Interactive session in the current folder: setup on first use, then prompt, run, review, merge. Type `/web` to toggle web access. |
| `tua --version`, `tua --help` | Version and help (`tua <command> --help` for each command) |
| `tua doctor` | Diagnose Python, Git, Claude Code (version, flags, login), Codex, local state, projects |
| `tua project init <PATH> --name <NAME> [--allow CMD ...]` | Create `.tua/project.yaml` if missing (never overwrites), validate, register or re-approve |
| `tua project list` / `tua project show <NAME>` | List projects; show config, skills, hashes, permission rules |
| `tua run "<PROMPT>" --project <NAME> [--provider P] [--model M] [--effort E] [--timeout S] [--max-turns N] [--profile read-only] [--web]` | Run one task (`--profile` can only narrow the project's permissions; `--web` allows web search) |
| `tua review <TASK_ID> [--provider P] [--model M] [--effort E] [--focus TEXT]` | Have a model review a task's changes, as a recorded read-only task |
| `tua status [TASK_ID] [--project NAME] [--limit N]` | List tasks, or show one with its event log and reviews |
| `tua diff <TASK_ID> [--stat]` | Changes against the base commit (works for failed tasks too) |
| `tua merge <TASK_ID> [--yes] [--force] [-m MSG]` | Review and squash-merge locally; never pushes |
| `tua discard <TASK_ID> [--yes]` | Remove the task's worktree and branch after confirmation |
| `tua good <TASK_ID> [--note ...]` / `tua bad <TASK_ID> --reason ... [--note ...]` | Record feedback |
| `tua eval baseline <EVAL_YAML> [--replace]` | Run a suite and store an immutable baseline |
| `tua eval run <EVAL_YAML>` | Run a suite as a candidate and compare it with its pinned baseline |

Both `eval` commands also accept `--max-runs N`, `--max-seconds N`, `--yes`, and
`--keep-worktrees`.

`--yes` skips only the confirmation for completed tasks; the diff is still printed. `--force`
lets you merge a failed, timed-out, or cancelled task. It always asks twice, the second time by
typing the task ID, and never skips safety checks.

Exit codes: `0` success, `1` error, failed task, or incomplete eval, `2` usage error, `124` task
timeout, `130` cancelled.

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
  allow: ["python -m pytest"]  # command prefixes the AI's shell tool and eval suites may run
  deny: ["python -m pip"]
```

A skill file is Markdown with front matter and one `## must: <id>` or `## default: <id>` section
per rule. Global skills are listed in `~/.config/tua/config.yaml`. Any edit to `project.yaml`
must be approved again before the next task. Details and examples:
[docs/configuration.md](docs/configuration.md), [examples/](examples/).

## Safety model

- **Claude Code** tasks get a generated settings file loaded with `--setting-sources user`, so
  project and local settings from the repository (hooks, allow rules) cannot widen access. MCP
  servers are off (`--strict-mcp-config`). Critical denies are repeated with `--disallowedTools`.
  `WebSearch` and `WebFetch` are denied unless web access is on for the task.
  Claude Code tasks cannot read outside the copy, except a read-only surrounding folder that
  `tua` set up when you started it there.
- **Claude Code permissions are not an OS sandbox.** An allowlisted command such as a test
  runner can run any code from the repository.
- **Codex** tasks use Codex's OS sandbox: `safe` maps to `workspace-write` (writes only to the
  copy and `/tmp`, no network) and `read-only` to `read-only`. Codex has no per-command
  allowlist. Its web search is set explicitly (`web_search="disabled"`, or `"live"` with web
  access on). **The Codex sandbox does not restrict reads:** a Codex task can read any file your
  user can, and its conversation goes to the provider. TUA warns about this on every Codex task.
  Use Claude Code for sensitive machines.
- TUA never reads credential files and reads only the `loggedIn` flag from
  `claude auth status` (for Codex, only the exit status and leading words of
  `codex login status`). It refuses prompts that
  contain recognizable secrets and redacts known secret formats in logs on a best-effort basis.
- **Web access is an explicit choice.** With it on, web pages can carry prompt injection, and
  anything the AI read could be sent out in a search or URL. TUA tells the AI to treat web
  content as untrusted and to keep project content out of requests, but that is a prompt, not a
  barrier.
- Run TUA only on code you are willing to execute.

Full details: [docs/security.md](docs/security.md).

## Local state

TUA keeps its data outside your repositories and respects `XDG_*`:

| What | Where |
|---|---|
| Database, task logs, worktrees, compiled skills, eval artifacts | `~/.local/state/tua/` |
| Global config and global skills | `~/.config/tua/` |
| Temporary files | `~/.cache/tua/` |

Directories are created with mode `0700` and files with mode `0600`. Your projects contain only
the `.tua/` folder, which you can commit or keep local.

## Limitations

- Two providers, Claude Code and Codex. Gemini CLI is not supported: Google stopped serving
  personal and Google AI Pro/Ultra accounts through it on June 18, 2026.
- Each task is a single headless run with no follow-up conversation inside TUA. `tua status
  <ID>` shows how to resume the session in the provider's CLI.
- Codex does not report the model it actually used or a cost. TUA records the requested model
  and leaves the rest unknown.
- Merges only onto an unchanged base. If your branch moved, merge the kept `tua/<ID>` branch
  yourself, or discard and re-run.
- The AI's copy contains only committed files. Ignored files (such as a local toolchain or
  build output) are not in it.
- Permission rules use prefix matching and the provider's own semantics. They are not a
  security sandbox.
- Rate-limit and authentication errors are classified only from structured fields or
  distinctive messages. Anything else is reported as `provider_error`.
- Eval comparisons over a few LLM runs are noisy, and TUA reports no statistical significance.

## Documentation

- [User guide](docs/user-guide.md)
- [Architecture](docs/architecture.md)
- [Configuration](docs/configuration.md)
- [Task lifecycle and recovery](docs/task-lifecycle.md)
- [Security](docs/security.md)
- [Evaluations](docs/evaluations.md)
- [Decision log](docs/decisions.md)
- [Changelog](CHANGELOG.md)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Run `scripts/dev-check.sh` (format, lint, type check,
tests, build) before opening a pull request. Tests use temporary repositories and fake `claude`
and `codex` executables, so they need no account and no network. Report security issues as
described in [SECURITY.md](SECURITY.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).
