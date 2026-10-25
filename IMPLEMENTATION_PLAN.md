# TUA v0.1 — Implementation Plan

Working checklist for the v0.1 build. Design context lives in `docs/`; discovered
constraints and decisions are recorded in `docs/decisions.md`.

## Environment facts (verified, not assumed)

- Python 3.12 locally; package targets `>=3.10` (CI covers 3.10–3.13).
- Git 2.43. Required git features: `worktree`, `commit-tree`, `merge --ff-only --no-overwrite-ignore`.
- Claude Code CLI 2.1.301. `--append-system-prompt-file` and `--max-turns` are **hidden** from
  `--help` but accepted; TUA verifies the exact flag set per task with a zero-cost preflight
  (`claude -p <flags> < /dev/null` cannot reach the API because it has no input).
- `claude auth status --json` exposes a `loggedIn` boolean; TUA reads only that field.
- Plain `git merge --ff-only` silently overwrites *ignored* files in the target checkout;
  `--no-overwrite-ignore` makes git refuse instead. TUA always passes it.

## Milestones and acceptance tests

| # | Milestone | Acceptance tests |
|---|-----------|------------------|
| M1 | Package, XDG paths, config validation, SQLite schema (`PRAGMA user_version`), errors, `--version`, `--help`, `doctor`, `project init/list/show` | config validation (unknown keys, bad types, traversal); DB init, unsupported/future schema, corrupt file; CLI help/version; private file modes |
| M2 | Git repository checks, task IDs, worktrees, state machine, transactional events, stale-task reconciliation | invalid repo; dirty repo refused and untouched; detached HEAD refused; duplicate ID retry; failed worktree creation; illegal transitions rejected; interrupted runner reconciled to `failed` |
| M3 | Claude Code adapter: capability preflight, auth check, headless JSON invocation, process-group timeout/cancel, env scrubbing, error classification | fake `claude` executable: success, provider error, crash, invalid JSON, unauthenticated, unsupported flag, rate limit, timeout (grandchild killed), SIGINT cancel; argv never contains bypass flags |
| M4 | Skill parser/resolver/compiler, `must`/`default` precedence, source + compiled SHA-256 | stable hashes; hash changes on source edit and on compiler version; precedence/override; duplicate and conflicting IDs; malformed metadata; path traversal and symlink escape |
| M5 | `diff`, `merge` (squash via `commit-tree` + `merge --ff-only --no-overwrite-ignore`), `discard`, `good`, `bad` | merge success; rejected confirmation; dirty target; target advanced; ignored-file collision; unresolved conflicts in worktree; force-merge double confirmation; discard; cleanup failure retained; feedback bound to fingerprints and does not change state or skills |
| M6 | Eval runner: schema, allowlist validation, confirmation, pinned immutable baseline, candidate comparison, run/time budgets | schema errors; disallowed command refused before any run; baseline refuses overwrite, `--replace` keeps old; candidate never reruns baseline; budget stops before exceeding and reports coverage; pin mismatch refused |
| M7 | Redacted structured logs, docs, CI, `scripts/dev-check.sh`, clean-venv install check, live smoke test | ruff format/check, mypy, pytest, `python -m build`; install in fresh venv; one minimal live run proving prompt-file injection and settings deny rules |

## Out of scope for v0.1

Servers, dashboards, daemons, multi-provider routing, debate/consensus, RAG/embeddings,
fine-tuning, automatic skill learning, a GUI, and any adapter other than Claude Code.

## Status

All milestones M1–M7 are implemented. Verification is recorded in `docs/decisions.md`
(discovered constraints) and by the test suite (`scripts/dev-check.sh`). The live smoke test
(`tests/live/`) passed against Claude Code 2.1.301.
