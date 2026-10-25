# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [0.1.0] - Unreleased

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
- Documentation, CI workflow, `scripts/dev-check.sh`, and an opt-in live smoke test.
