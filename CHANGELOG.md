# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- GitHub Actions CI workflow on Python 3.10 to 3.13: format check, lint, type check, tests,
  build, and a smoke test of the built wheel.

### Fixed

- `tua review` and the interactive review now say when a read-only review left changed files
  in its worktree, or when its worktree copy could not be checked or removed. They show the
  path and how to inspect and remove it; nothing is merged or deleted automatically.
  `tua review --keep-worktree` prints the kept path.
- The interactive review reports a failed review (status, error, kept worktree) instead of an
  empty review.

## [0.1.0] - 2026-10-25

First tagged release.

### Fixed

- A project's `default_model` was passed to whichever provider ran the task. In a Claude Code
  project with `default_model: sonnet`, `tua run --provider codex` and
  `tua review --provider codex` sent `--model sonnet` to Codex, which rejects it ("The 'sonnet'
  model is not supported when using Codex with a ChatGPT account"). `default_model`, like
  `effort`, now applies only to the project's own `default_provider`. Another provider gets
  its CLI default unless `--model` is given, and TUA prints a note when a project setting is
  left out.

### Added after the first real-use test run

- `tua review <ID> [--model M] [--focus TEXT]`: a recorded, read-only review task linked to
  the reviewed task (schema migration 2 adds `tasks.review_of`).
- `tua run --profile read-only`: per-task profile that can only narrow the project's profile.
- `tua project init --allow CMD`: seed `commands.allow` (for example the test command).
- The full redacted provider answer is saved as `result.md` in the task directory.
- Cost is shown as an API-equivalent estimate; subscriptions are not billed per token.
- Codex CLI adapter (`default_provider: codex` or `--provider codex`):
  - skills passed as developer instructions
  - permissions from Codex's OS sandbox
  - the same zero-cost preflight and lifecycle handling as Claude Code
- `effort` setting and `--effort` flag, validated per provider and recorded per task (schema
  migration 3).
- `tua review --provider` enables cross-provider reviews, for example Codex reviewing a Claude
  task.
- `tua doctor` also checks Codex when it is installed.
- Interactive mode: `tua` with no arguments inside a project folder.
  - First use offers setup with yes/no questions: register the project, allow a detected test
    command, keep `.tua/` local or commit only that folder.
  - It then loops: prompt, run, then merge / diff / review / discard / keep.
  - Settings that came with a repository, and changed settings, must be approved first.
- `tua` works from any folder.
  - Inside a folder that *contains* Git projects, it offers to use one.
  - In a plain folder, it shows exactly what a first commit would include and offers to create
    the repository. Files over 50 MB, secret-like files, and nested projects are left out and
    added to `.gitignore`. It refuses for the home folder, top-level folders, and very large
    folders.
  - When the checkout has uncommitted files, it offers to commit them (never secrets or large
    files) or to hide untracked leftovers in `.git/info/exclude`, then continues. A build folder
    is hidden as a whole only when it contains no tracked files.
- Like `claude`, `tua` works from where it is started.
  - From a folder around a project, the AI can read that whole folder, but only edits its Git
    copy. Claude Code gets `--add-dir` plus `Read` allow and `Edit` deny rules for it, and the
    prompt says where the user started.
  - From a project subfolder, the AI is told to focus there.
  - A project chosen inside a folder is remembered.
  - Home and system folders are never used as context.
  - Schema migration 4 records `context_dir` and `start_dir`.
- Opt-in web access: `tua run --web`, `/web` in `tua`, or a yes/no question when a request
  mentions the internet.
  - Claude Code tasks get `WebSearch`/`WebFetch` only then.
  - Codex tasks now always set `web_search` explicitly (`disabled` or `live`).
  - Schema migration 5 records `tasks.web`, and `tua status` shows it.
- `tua` prints the AI's full answer (up to 20,000 characters) instead of the first 800.
- The provider prompt says how to list and search files with simple read-only commands.

### Added

- `tua` CLI:
  - `doctor`
  - `project init/list/show`
  - `run`, `status`, `diff`, `merge`, `discard`
  - `good`, `bad`
  - `eval baseline`, `eval run`
- SQLite state with schema versioning; a validated task state machine with transactional
  events; append-only events and feedback; immutable eval baselines.
- One Git worktree per task, created from a recorded base commit. Dirty checkouts are refused.
- Claude Code adapter:
  - headless JSON mode, prompt on stdin
  - zero-cost flag preflight
  - login check that reads only `loggedIn`
  - process-group timeout and cancellation
  - normalized results and error types
- Skill compiler: `must`/`default` precedence, conflict detection, separate source and compiled
  SHA-256 fingerprints, payload injected with `--append-system-prompt-file`.
- Permission profiles `safe` and `read-only`: `dontAsk` mode, worktree-scoped edits, command
  allowlist, web tools denied, repository settings and MCP servers not loaded.
- Squash merge onto an unchanged base, with full diff review and confirmation. Force-merges of
  unsuccessful tasks need a second, typed confirmation.
- Eval runner: allowlist-validated commands, run-count and wall-clock budgets, pinned
  baselines, candidate comparison.
- Best-effort secret redaction, and refusal of prompts that contain recognizable secrets.
- Documentation, `scripts/dev-check.sh`, and an opt-in live smoke test.
