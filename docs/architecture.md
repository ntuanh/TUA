# Architecture

TUA is a thin, synchronous CLI. There is no server, daemon, queue, or background process: each
`tua` command runs, does its work, and exits. One task equals one headless provider execution.

```text
tua (cli.py, interactive.py) ──► project registry (projects.py, config.py, gitsetup.py)
      │                 │
      │          skill compiler (skills.py) ──► compiled payload + SHA-256s
      │                 │
      ├──► task service (tasks.py) ──► permission builder (permissions.py)
      │         │                 └──► worktree manager (worktrees.py, repository.py)
      │         └──► provider adapters ──► adapters/claude_code.py ──► `claude --print`
      │                                    adapters/codex.py ──────► `codex exec --json`
      │                                    └── process runner (process.py)
      ├──► review (review.py): diff / merge / discard; peer review (peer_review.py)
      ├──► feedback (feedback.py)
      ├──► evaluations (evaluations.py)
      └──► doctor (doctor.py)
                         all state ──► SQLite (db.py) + private files under XDG state
```

## Modules

| Module | Responsibility |
|---|---|
| `cli.py` | Argument parsing, rendering, confirmations. No SQL and no business rules. |
| `interactive.py` | The `tua` session: finds the project from the current folder, first-run setup, prompt loop, merge/diff/review/discard menu. |
| `gitsetup.py` | Finding Git projects inside a folder, turning a plain folder into one (leaving out large, secret-like, and nested files), hiding leftovers locally. |
| `config.py` | Strict parsing of `.tua/project.yaml` and the global config; path validation. |
| `projects.py` | `project init` and loading registered projects with config-fingerprint checks. |
| `db.py` | Schema migrations (`PRAGMA user_version`), transactions, and all SQL. |
| `models.py` | Task states, the allowed-transition table, error types, record types. |
| `repository.py` | The one way TUA runs Git (argument lists, scrubbed environment, timeouts). |
| `worktrees.py` | Worktree creation, temporary-index snapshots, diffs, validated removal. |
| `skills.py` | Skill parsing, precedence resolution, deterministic compilation, hashing. |
| `permissions.py` | Permission profiles to Claude Code allow/deny rules; eval command checks. |
| `adapters/base.py` | The provider contract: `AdapterRequest` in, `AdapterResult` out. |
| `adapters/claude_code.py` | Claude Code: capability preflight, login check, invocation, normalization. |
| `adapters/codex.py` | Codex: the same contract, using `codex exec --json` and Codex's sandbox. |
| `peer_review.py` | `tua review`: read-only review tasks linked to the reviewed task. |
| `process.py` | Process-group execution with timeout, cancellation, and bounded output. |
| `tasks.py` | Task creation, preparation, execution, result recording, reconciliation. |
| `review.py` | `diff`, `merge`, and `discard`, with all safety checks. |
| `feedback.py` | Append-only, task-bound feedback. |
| `evaluations.py` | Eval schema, budgets, baselines, candidate comparison. |
| `logging_utils.py` | Best-effort redaction and the JSON-lines log. |
| `doctor.py` | Environment diagnostics. |

## Task flow

1. Validate the prompt. Recognizable secrets are refused.
2. Load the registered project. Refuse if `.tua/project.yaml` changed since registration.
3. Check that the source checkout is a clean repository root on a branch. Record the branch and
   the `HEAD` commit.
4. Insert the task (`created` then `preparing`, atomically), recording the runner's PID identity.
5. Compile the skills and store the payload content-addressed (`compiled/<sha256>.md`, `0600`).
   Generate the permission settings file for the planned worktree path.
6. Adapter preflight: `claude --version`, the exact task argv with stdin set to `/dev/null`
   (no API call is possible), and `claude auth status --json`.
7. `git worktree add -b tua/<id> <state>/worktrees/<id> <base>`, then verify `HEAD == base`.
8. Re-verify the compiled payload hash, move to `running`, and start `claude --print` in the
   worktree with the prompt on stdin.
9. Normalize the result and record it, with its event, in one transaction.

## Adapter contract

`AdapterRequest`:

- task ID and prompt
- absolute worktree path
- compiled skill path and hash
- settings path and permission mode, plus CLI-level deny rules
- output directory
- timeout
- optional model alias and max turns

`AdapterResult`:

- normalized status, exit code, start/end timestamps, duration
- stdout/stderr log paths and a redacted result excerpt
- CLI version and the configured model alias
- reported model IDs (from `modelUsage`)
- session ID, turns, cost and usage as reported by the CLI
- permission denials
- error type and message

Any value the CLI does not supply reliably is `None` ("unknown"). A provider implements `detect`,
`check_ready`, `login_state`, and `run`. v0.1 ships Claude Code and Codex.

## Data model

All tables are created by migration 1. Migrations 2 to 5 add task columns: `review_of`
(the task a review belongs to), `effort`, `context_dir` / `start_dir` (the read-only
surrounding folder and the focus subfolder set by `tua`), and `web` (whether web access was
on).

| Table | Contents |
|---|---|
| `projects` | Name, root, config path, registered config fingerprint. |
| `tasks` | Identity (immutable by trigger): prompt, base commit, branches, paths. Fingerprints: skill hashes, config fingerprint, CLI version, model alias and reported models. Outcome: timestamps, exit code, error type and message, logs, session ID, usage, permission denials. Process identities used for crash detection, plus merge and cleanup results. |
| `task_events` | Append-only (enforced by trigger) state transitions and notable events: refused merges, cleanup failures. |
| `feedback` | Append-only rating, reason, and note, plus a snapshot of the task's fingerprints. |
| `eval_runs`, `eval_case_runs` | Budgets, usage, per-run outcomes, metrics, artifact paths. |
| `eval_baselines` | Immutable (enforced by trigger) baseline records pointing to a read-only manifest and its SHA-256. |

A database with a newer schema, a corrupt file, or an unrelated SQLite file is refused with
instructions. TUA never recreates or deletes it.
