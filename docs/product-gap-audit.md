# Product gap audit

| | |
|---|---|
| Date | 2026-10-25 |
| Code audited | `main` at `7974303`. The last pushed commit is `3e72323`; `7974303` only adds the CI workflow. |
| Scope | `src/`, `tests/`, `docs/`, `README.md`, `IMPLEMENTATION_PLAN.md`, and the original brief in `plan/` (untracked) |
| Companion | [target-architecture.md](target-architecture.md) |

This audit compares what TUA does today with the broader product vision: a personal terminal
assistant that feels like Claude Code, with personal knowledge, project skills, optional agent
teams, automatic model routing, verified web research, and controlled learning from feedback.
It is an assessment and a plan. No code was changed.

## 1. Summary

- **What TUA is today.** TUA is a careful, well-tested runner for one headless coding task at a
  time. It runs Claude Code or Codex in a fresh Git worktree, records everything about the run,
  and lets the user diff, merge, review, or discard the result. Its safety core is its strongest
  part: worktrees, merge checks, permission plans, fingerprints, and append-only records. Keep
  it as it is.
- **What it is not yet.** TUA is not a conversational assistant. Every request becomes a coding
  task in a new Git worktree, starting from the last commit, with no memory of earlier
  requests. That includes a question about the code and a web search.
- **Vision coverage.** None of the ten vision areas is fully met. Seven are partly met and three
  are absent (see [section 5](#5-comparison-with-the-vision)).
- **The biggest mismatch** is that the Git worktree, built for safe edits, is the entry point
  for everything. Asking a question needs a clean checkout, and starting in a plain folder means
  turning it into a Git project first.
- **Recommended direction.** Do not rewrite. Add two layers on top of the existing task core:
  1. A **mode** layer: ask, knowledge, or code.
  2. A **conversation** layer: follow-up turns that keep context.

  Then add, in this order: routing, research provenance and knowledge, the feedback-to-skill
  loop, and finally optional orchestration.
- **Gemini** cannot use the user's Google AI Pro subscription:
  - Gemini CLI rejects personal accounts.
  - Antigravity CLI's terms forbid wrapping it in another app on consumer plans.

  The only route is a paid Gemini API key, which is billed per request, separately from the
  subscription (see [section 6.5](#65-routing-is-manual-and-gemini-is-unavailable)).

## 2. Method and evidence

| Check | Result |
|---|---|
| `ruff format --check`, `ruff check` | Pass (62 files) |
| `mypy` (strict) | Pass, 28 source files |
| `pytest` | **428 passed, 2 skipped** (the two live tests), 155 s |
| `python -m build` | sdist and wheel built |
| Live tests ([tests/live/](../tests/live/)) | **Not run.** They spend subscription quota. Live results recorded in [decisions.md](decisions.md) are cited below as "verified live earlier". |
| CI on GitHub | **Has never run.** [ci.yml](../.github/workflows/ci.yml) (Python 3.10 to 3.13) is committed locally in `7974303`, but not pushed, because the GitHub token lacks the `workflow` scope. |
| Installed CLIs | Claude Code 2.1.310 and Codex 0.164.0: flags read from `--help`, no requests sent. Gemini CLI 0.65.0: a one-line request with the user's Google AI Pro login was rejected (`IneligibleTierError`, `UNSUPPORTED_CLIENT`). |

Size: 7,868 lines of Python in `src/tua` (28 modules), 5,008 lines of tests (430 tests), and one
runtime dependency (PyYAML). The checks ran with `PYTHON=.venv/bin/python scripts/dev-check.sh`.

Status labels:

| Label | Meaning |
|---|---|
| **Tested** | Implemented, and exercised by automated tests that use real Git and fake provider CLIs. |
| **Tested + live** | Also verified once against the real CLI, as recorded in the decision log. Not re-run for this audit. |
| **Limited** | Implemented, but narrower than the vision or with a gap named in the row. |
| **Claimed** | Stated in the documentation, but no code or test backs it. |
| **Absent** | No code. |

## 3. Current architecture

TUA is a synchronous Python CLI with no server or daemon. Every path goes through one service,
`TaskService.run` ([tasks.py:185](../src/tua/tasks.py#L185)):

```text
tua (interactive.py)  or  tua run (cli.py)
 └─ TaskService.run ─────────────────────────────────────────── tasks.py
     ├─ load the registered project; refuse a changed config ── projects.py, config.py
     ├─ require a clean checkout; record branch and HEAD ─────── repository.py
     ├─ compile skills into one payload, with two SHA-256s ──── skills.py
     ├─ build the permission plan (Claude Code only) ────────── permissions.py
     ├─ preflight: version, exact argv with empty stdin, login ─ adapters/
     ├─ git worktree add -b tua/<id> at the recorded base ───── worktrees.py
     └─ run the provider headless, capture, normalize, record ── process.py, db.py
then:  diff / merge / discard (review.py) · review (peer_review.py)
       good / bad (feedback.py) · eval baseline / run (evaluations.py) · doctor (doctor.py)
```

Commands: `tua` (interactive), `doctor`, `project init/list/show`, `run`, `review`, `status`,
`diff`, `merge`, `discard`, `good`, `bad`, `eval baseline`, `eval run`. The
[README](../README.md#command-reference) lists their flags.

State lives in SQLite at schema version 5 ([db.py](../src/tua/db.py)), in these tables:

- `projects`
- `tasks` (identity columns immutable by trigger)
- `task_events` and `feedback` (append-only)
- `eval_runs` and `eval_case_runs`
- `eval_baselines` (immutable)

Files live under the XDG directories, with modes `0700` and `0600`.

## 4. Feature status matrix

### Tasks, Git, and safety

| Feature | Status | Evidence |
|---|---|---|
| Durable task IDs, validated state machine, transactional events | Tested | [models.py](../src/tua/models.py), [db.py](../src/tua/db.py); `test_db`, `test_task_lifecycle` |
| One worktree per task from a recorded base; dirty checkout refused, never altered | Tested | [tasks.py:195](../src/tua/tasks.py#L195), [worktrees.py](../src/tua/worktrees.py); `test_dirty_checkout_is_refused_and_untouched` |
| Timeout, Ctrl+C, process-group kill, crash reconciliation | Tested | [process.py](../src/tua/process.py); `test_process` (12), `test_timeout_keeps_worktree_and_kills_provider`, `test_interrupted_runner_is_reconciled` |
| Diff, squash merge (`commit-tree` + `--ff-only --no-overwrite-ignore`), discard, double-confirmed force merge | Tested | [review.py](../src/tua/review.py); `test_review` (22 tests) |
| Config fingerprint: a changed `project.yaml` needs re-approval | Tested | [projects.py](../src/tua/projects.py); `test_changed_settings_need_approval` |
| Command allowlist validation (no shells, wildcards, broad tools) | Tested | [config.py](../src/tua/config.py); `test_config` |
| Secret refusal in prompts and feedback; best-effort log redaction | Tested | [logging_utils.py](../src/tua/logging_utils.py); `test_redaction`, `test_secret_in_prompt_is_refused` |

### Providers and models

| Feature | Status | Evidence |
|---|---|---|
| Claude Code adapter: zero-cost preflight, `loggedIn`-only login check, JSON result, error classes | Tested + live | [adapters/claude_code.py](../src/tua/adapters/claude_code.py); `test_claude_adapter`; D1 to D3 |
| Claude Code permission plan: `dontAsk`, generated settings, `--setting-sources user`, no MCP | Tested + live | [permissions.py](../src/tua/permissions.py); `test_permissions`; D3 |
| Codex adapter: OS sandbox per profile, skills as developer instructions, explicit `web_search` | Tested + live | [adapters/codex.py](../src/tua/adapters/codex.py); `test_codex_adapter`, `test_codex_tasks`; D18, D21 |
| Provider, model, and effort per project or per task | Tested, **manual only** | `RunOptions` in [tasks.py](../src/tua/tasks.py#L72); `--provider/--model/--effort` |
| Task complexity classification, effort allocation, model routing | **Absent** | No code |
| Gemini | **Absent** (blocked for consumer accounts) | See [section 6.5](#65-routing-is-manual-and-gemini-is-unavailable) |

### Skills and configuration

| Feature | Status | Evidence |
|---|---|---|
| `must`/`default` rules, precedence, conflict detection, source and compiled hashes | Tested | [skills.py](../src/tua/skills.py); `test_skills` (17 tests) |
| Project skills, created by `project init` | Tested | [templates/project-skill.md](../src/tua/templates/project-skill.md); `test_project_init` |
| Global skills that apply to every project | **Limited** | Resolved in [skills.py:144](../src/tua/skills.py#L144) and unit-tested. No test runs a global skill through a task. No command creates or manages `~/.config/tua/config.yaml`; it is edited by hand. |
| Skill version history | **Limited** | Compiled payloads are kept by hash in `state/compiled/`, and every task records both hashes. TUA keeps no copy of the *source* files. Interactive setup keeps `.tua/` out of Git by default ([interactive.py:279-288](../src/tua/interactive.py#L279-L288)), so project skills then have no history at all. |
| Project skill edits need re-approval | **No, by design** | The fingerprint covers `project.yaml` only. Skill text is prompt guidance, not permissions, but a `git pull` can change it silently. The new hash is still recorded. |

### Interactive session

| Feature | Status | Evidence |
|---|---|---|
| `tua` in any folder: first-run setup, folder detection, offer to create a Git repo, tidy-up of leftovers | Tested | [interactive.py](../src/tua/interactive.py), [gitsetup.py](../src/tua/gitsetup.py); `test_interactive` (19), `test_gitsetup` |
| Read-only context folder around the project | Tested + live | [tasks.py:94](../src/tua/tasks.py#L94); `test_context_dir`; D20 (the `Edit` deny itself was not triggered live) |
| Full answer printed and saved as `result.md` | Tested | `test_interactive_shows_the_full_answer` |
| Follow-up turns that keep context | **Absent** | Each prompt starts an independent task from `HEAD` ([interactive.py:342](../src/tua/interactive.py#L342)). |
| Progress while the AI works | **Absent** | Output is buffered until the process exits ([process.py:230](../src/tua/process.py#L230)). The default timeout is 1,800 s. |

### Quality and learning loop

| Feature | Status | Evidence |
|---|---|---|
| Peer review as a recorded read-only task, also across providers | Tested | [peer_review.py](../src/tua/peer_review.py); `test_peer_review` (9) |
| `good`/`bad` feedback bound to the task's fingerprints | Tested, **write-only** | [feedback.py](../src/tua/feedback.py). Shown only per task in `tua status <ID>`. Nothing lists, aggregates, or uses it. |
| Eval suites with pinned, immutable baselines and run/time budgets | Tested | [evaluations.py](../src/tua/evaluations.py); `test_evaluations` (10), `test_eval_schema` (7) |
| Candidate evaluation of a skill change | **Limited** | The candidate is whatever skill files are on disk (`require_clean=False`, [evaluations.py:814](../src/tua/evaluations.py#L814)). Suites and baseline pins are written by hand. |
| Feedback leads to a skill proposal, evaluated, then approved | **Absent** | No proposal object, link, or approval step |

### Research and knowledge

| Feature | Status | Evidence |
|---|---|---|
| Opt-in web search and fetch, recorded per task | Tested + live | `test_web_access` (6); D21. Codex with web off was not run live. |
| "Research with sources" ([README](../README.md#several-models-one-workflow)) | **Claimed** | TUA does not capture, store, or check sources. Citations exist only if the model writes them in its answer. |
| Source metadata, citation verification, review inbox, promotion into knowledge | **Absent** | No code |
| Project or personal memory and knowledge notes | **Absent** | No code |

### Orchestration

| Feature | Status | Evidence |
|---|---|---|
| Planner, workers, researcher, and reviewer team; task graph | **Absent** | Only the single peer reviewer exists. |
| Plan and progress view | **Absent** | No code |

### Platform

| Feature | Status | Evidence |
|---|---|---|
| Linux | Tested locally | Python 3.12 here; CI for 3.10 to 3.13 has never run |
| macOS | **Claimed** | "should mostly work but is untested" ([README](../README.md#1-install-once-per-machine)) |

## 5. Comparison with the vision

| # | Vision | Today | Status |
|---|---|---|---|
| 1 | A natural interactive session, easier than commands | `tua` gives one prompt and one task, then a menu. There are no follow-ups and no progress. | Limited |
| 2 | Global personal skills and local documents across projects | Global skills work if configured by hand. Personal documents are not supported. | Limited |
| 3 | Project-local config, skills, deep knowledge, memory, feedback | Config, skills, and feedback exist. Knowledge and memory do not. | Limited |
| 4 | Global-to-project inheritance with conflict resolution and version history | Inheritance and conflict rules are tested. History covers compiled payloads only. | Limited |
| 5 | Optional project agent teams (planner, workers, researcher, reviewer) | One reviewer, run by hand | Absent |
| 6 | A visible plan and progress view | None | Absent |
| 7 | Complexity classification, effort allocation, routing across Claude, Codex, and Gemini | Manual choice between two providers; Gemini is blocked | Absent |
| 8 | Web research, source metadata, citation checks, Good/Bad inbox, knowledge promotion | Web search only | Limited |
| 9 | Feedback-driven skill proposals evaluated against baselines before approval | Both ends exist (feedback, baselines); the middle does not | Limited |
| 10 | Low friction: a direct path for simple tasks; orchestration optional | The direct path exists but is heavy: a worktree and a clean checkout for every request | Limited |

## 6. Major mismatches

### 6.1 Every request is a coding task in a Git worktree

`TaskService.run` refuses a dirty checkout for every task, including `--profile read-only`
([tasks.py:195](../src/tua/tasks.py#L195)). It then creates a branch and a worktree from `HEAD`.
The consequences:

- A question about the code is answered from the **last commit**, not from the files the user
  is editing.
- A question in a dirty checkout is refused. In interactive mode the user is pushed to commit
  everything as "Work in progress (saved before a TUA task)" with `git add -A`
  ([interactive.py:405-411](../src/tua/interactive.py#L405-L411)), or to hide files in
  `.git/info/exclude`.
- Research in a plain folder (for example a folder of PDFs) first requires turning it into a
  Git project ([interactive.py:207](../src/tua/interactive.py#L207)).
- Large repositories pay for a full checkout per question.
- Kept tasks leave `tua/<id>` branches and worktrees behind. They are removed one at a time with
  `tua discard`.

### 6.2 No conversation

Each prompt in `tua` is a new task. It cannot see the previous answer, and if the previous change
was kept but not merged, it cannot see that change either. Both CLIs support follow-ups in
headless mode: `claude --resume`/`--continue`, and `codex exec resume`. TUA records the provider
session ID but uses it only to print a hint ([cli.py:368-379](../src/tua/cli.py#L368-L379)).

### 6.3 No knowledge layer

There is no place for:

- personal notes or documents that apply across projects
- curated project knowledge
- memory

The only ways to give the AI context are:

- the repository's committed files
- skill text
- the read-only folder the user started in (Claude Code `--add-dir`). This is enforced only for
  Claude Code; Codex can read any file the user can.

### 6.4 Skills: inheritance yes, lifecycle no

Precedence and conflict handling are solid and tested. Missing:

- a way to list, edit, or diff skills from TUA
- history of source files
- proposals and approvals

Changes to skill files are visible only as new hashes.

### 6.5 Routing is manual, and Gemini is unavailable

The provider, model, and effort come from the project config or flags. Nothing estimates task
complexity, picks a cheaper model for simple work, or falls back when a plan's limit is
reached. A rate limit just fails the task (`rate_limited`).

Gemini, checked on 2026-10-25:

| Route | Billing | Status |
|---|---|---|
| Gemini CLI, Google login (`oauth-personal`) on Google AI Pro | Subscription | **Rejected.** Google stopped serving personal, AI Pro, and AI Ultra accounts on June 18, 2026; reproduced with the user's account. |
| Antigravity CLI (`agy`) on a consumer plan | Subscription | **Excluded.** Its FAQ says wrapping `agy` inside third-party apps is not supported on consumer plans and can lead to account suspension. |
| Gemini CLI with a paid Gemini API key or Vertex AI | **API, per request** | Possible; not tested. Google's announcement says free unpaid keys moved to Antigravity CLI too. |

Sources: [Google Developers Blog](https://developers.googleblog.com/an-important-update-transitioning-gemini-cli-to-antigravity-cli/),
[Antigravity FAQ](https://antigravity.google/docs/faq/),
[Gemini CLI quotas](https://geminicli.com/docs/resources/quota-and-pricing/).

### 6.6 Research without provenance

With web access on, the provider can search and fetch pages. TUA does not ask for sources in a
structured form, store them, check that a quoted passage exists, or ask a person to confirm
anything. The README phrase "Research with sources" overstates this.

### 6.7 Feedback dead-ends

`tua good`/`bad` writes careful, fingerprinted, append-only records. Nothing reads them back
across tasks, groups them by skill, or proposes changes. The eval runner can compare a changed
skill with a baseline, but the user has to make the change, write the suite, and paste the pin.

### 6.8 Read-only Claude Code tasks cannot list or search files

The `read-only` profile denies `Bash` entirely ([permissions.py:122](../src/tua/permissions.py#L122)).
The D21 live probe found that Claude Code 2.1.310 has no `Glob` or `Grep` tool and lists and
searches only through `Bash`. Together, these mean that a read-only Claude task, including every
`tua review`, can read only files whose paths it already knows. The `[TUA]` listing note is
added only for the `safe` profile ([tasks.py:128](../src/tua/tasks.py#L128)). This is inferred
from the code and D21; confirm it with a live probe before relying on it.

## 7. User-experience friction

Each item below was found in the code.

1. **No streaming feedback.** After "Running claude-code … Press Ctrl+C to cancel." the screen
   stays silent until the provider exits.
2. **A clean checkout for every request.** Even an explanation needs it, and the interactive fix
   adds commits to the user's branch.
3. **Answers about stale code.** Read-only questions see `HEAD`, not the working files.
4. **No follow-ups.** "Now also add a test" starts from scratch unless the previous task was
   merged first.
5. **Git setup for non-code folders.** Research in a documents folder requires `git init`.
6. **Manual model choice.** The user needs to know which provider, model, and effort fit the
   task. Only the docs give guidance.
7. **Clutter.** Every kept task leaves a branch and a worktree, and there is no bulk cleanup or
   "kept tasks" view.
8. **Hand-written configuration** for global skills, evals, and baseline pins.
9. **Feedback has no visible effect,** which discourages giving it.
10. **The web question is a keyword heuristic** ([interactive.py:43](../src/tua/interactive.py#L43)).
    That is acceptable, but it can ask needlessly ("browse the code") or miss a need ("latest
    version of X").

Things that already work well and should stay:

- the one-key menu after a task
- first-run setup with safe defaults
- refusal messages that explain what to do
- the full answer saved in `result.md`
- the `tua run` command surface for scripting

## 8. Git and worktree assessment

### What needs a worktree

| Operation | Needs a worktree? | Why |
|---|---|---|
| Edit code or project files for later review | **Yes** | Isolation from the user's checkout, an exact diff, undo, and a safe merge. Keep the current design unchanged. |
| Run tests or build commands that write files | **Yes** | Writes stay in the copy. Claude Code's allowlist and Codex's sandbox both assume it. |
| Evaluate a skill (eval runs) | **Yes** | Each run needs a pristine, pinned commit and its own writable copy. |
| Review a task's change | Not a new one | The result already lives in the task's worktree (or the merged commit). A read-only run there avoids a second checkout. Today `tua review` creates a snapshot worktree. |
| Explain, answer, or analyze code (read-only) | **No** | Nothing is written. Running read-only in the user's folder answers about the files the user actually has, and needs no clean checkout. |
| Research on the web, with or without local files | **No** | Writes nothing to the user's files. A TUA-owned scratch folder is enough as the working directory. |
| Draft documents or notes (knowledge mode) | **No Git needed** | Writes go to a TUA-owned staging folder, and the user applies them explicitly (see below). |

### Recommended modes

| Mode | Working directory | Writes | Git effect | Isolation |
|---|---|---|---|---|
| **Ask** (conversation, research, read-only analysis) | The user's folder (Git or not), or a TUA scratch folder | None in the user's files | None: no clean check, branch, or worktree | Claude Code: read-only plan plus secret-file read denies. Codex: `--sandbox read-only`. |
| **Knowledge** (documents, notes, reports) | A TUA staging folder; source folders readable | Only in staging; applied to the user's folder by explicit, no-overwrite copy | None, unless the target is in a repo and the user commits | Same read rules; writes confined to staging |
| **Code** (today's tasks) | Task worktree | Worktree only | `tua/<id>` branch; squash merge on confirmation | Unchanged |

**Constraints on the change:**

- Keep every worktree protection in code mode.
- Never move a writing task out of a worktree when the target is a Git project.
- Make the mode visible on screen and in the database.

### Files outside Git: untracked documents, PDFs, other folders

- **Read in place, never copied.** Grant read-only access to explicitly chosen roots: the start
  folder, configured "library" folders, and knowledge folders. For Claude Code, use `--add-dir`
  plus `Read` allow and `Edit` deny rules. Codex reads anything regardless, so its tasks must be
  labelled accordingly (today they are). Claude Code's `Read` tool can read PDFs. TUA should
  not convert or upload files itself.
- **Exclude secrets when reading live folders.** A worktree contains only committed files. A
  live folder also has `.env`, keys, and other untracked secrets. Ask mode should add `Read`
  denies for the same secret patterns that [gitsetup.py](../src/tua/gitsetup.py) already
  recognizes, on top of the home-directory credential denies.
- **Never write in place by default.** Knowledge-mode output goes to staging. Applying it copies
  each file to its target only after showing the list:
  - It refuses to overwrite unless the user confirms each overwritten file.
  - It keeps a backup copy.
  - It records the file hashes.

  Nothing is deleted.
- **Keep large and binary files out of Git.** As today, they are referenced by path, never
  committed or offered for commit.
- **Do not require `git init` for reading.** Offer it only when the user switches to code mode
  in a plain folder, as today.

## 9. Reusable modules

| Module | Reuse in the target design |
|---|---|
| [process.py](../src/tua/process.py) | Unchanged process-group runner. Add an optional per-line callback for streaming events. |
| [adapters/](../src/tua/adapters/) | Keep. Extend `AdapterRequest` with a working directory (not only a worktree), a session to resume, an output schema, and an event callback. |
| [skills.py](../src/tua/skills.py) | Keep the format, precedence, and hashes. Add a source-snapshot store and compilation from an overlay (for proposals). |
| [permissions.py](../src/tua/permissions.py) | Keep. `build_plan` already takes the working path and the context roots, so it can build an ask-mode plan for a live folder. Add secret-file denies and optional read-only listing. |
| [tasks.py](../src/tua/tasks.py) | Keep for code mode. Extract the "prepare, preflight, run, record" part of `_execute` into a reusable provider-run step. |
| [worktrees.py](../src/tua/worktrees.py), [review.py](../src/tua/review.py) | Unchanged for code mode. Plan steps can chain worktrees with the `snapshot_tree` + `commit-tree` technique already used in `peer_review._snapshot`. |
| [peer_review.py](../src/tua/peer_review.py) | Becomes the reviewer role of a plan. |
| [feedback.py](../src/tua/feedback.py), [evaluations.py](../src/tua/evaluations.py) | Ends of the learning loop. Add listing and grouping, and evaluation with a proposed skill overlay. |
| [db.py](../src/tua/db.py) | Same migration mechanism; new tables only (additive). |
| [gitsetup.py](../src/tua/gitsetup.py) | Secret-name detection reused for ask-mode read denies. |
| [interactive.py](../src/tua/interactive.py) | Becomes the session front end: keeps setup and the menu, gains modes and follow-ups. |
| [logging_utils.py](../src/tua/logging_utils.py), [paths.py](../src/tua/paths.py) | Unchanged |
| Test fixtures ([fake_claude.py](../tests/fixtures/fake_claude.py), [fake_codex.py](../tests/fixtures/fake_codex.py), [conftest.py](../tests/conftest.py)) | Extend with stream events, resume, and structured output. Every new feature can be tested without quota. |

## 10. Proposed target architecture

The full design is in [target-architecture.md](target-architecture.md). In short:

- **A session manager** keeps a conversation of turns. Ask turns resume the provider session;
  code turns can continue in the same task worktree.
- **A task analyzer** (rules first) picks the mode and the task class, shows its reason, and lets
  the user override it with one key.
- **A provider router** picks a route (provider, model, effort, billing kind) from routes the
  user configured. It falls back on a rate limit, and never uses API billing unless the user
  enabled it.
- **An optional planner** produces a plan the user approves. Its steps are ordinary TUA runs,
  executed in sequence, with a terminal progress view.
- **A research source registry, an inbox, and knowledge notes** (Markdown files) provide
  verified research. Nothing becomes knowledge without a person's approval.
- **Skill proposals** are drafted from feedback, evaluated against the pinned baseline with an
  overlay, and applied only after approval.
- **Persistence and runtime stay as they are:** SQLite and XDG files, with additive migrations.
  No server, daemon, dashboard, or vector database.

## 11. Roadmap

Each phase ships on its own and keeps the existing CLI behavior. "Existing tests pass unchanged"
applies to every phase.

### Phase 0: Fix what is wrong today (small)

- Correct the documentation drift listed in the [appendix](#appendix-documentation-drift).
- Add an integration test showing that a global skill reaches a task's compiled payload.
- Probe live whether a `read-only` Claude Code task can list files. If it cannot, decide
  whether `read-only` should rely on `dontAsk`'s built-in read-only command handling instead of
  denying `Bash` (see D21).

**Acceptance:**

- The docs match the code.
- A new test passes using the fake CLI.
- The probe result is recorded in `decisions.md`.

### Phase 1: Ask mode without a worktree

- Add `tua ask "<question>"`, and an ask path in `tua`. It runs read-only in the user's folder,
  or in a TUA scratch folder when started outside a project.
- It works in a dirty checkout, a plain folder, or a project subfolder.
- Each ask is recorded as a turn (a new table, not `tasks`; see the target design).

**Acceptance (integration tests with the fake CLIs):**

- Ask in a dirty repository answers. It leaves `git status`, `git branch --list`, and
  `git worktree list` byte-identical.
- Ask in a plain folder works without `git init`.
- Claude Code ask runs with `Edit`, `Write`, and `NotebookEdit` denied, secret-file `Read`
  denies, and web per toggle.
- Codex ask uses `--sandbox read-only`.
- The turn is recorded with mode, working directory, route, and fingerprints, and `tua status`
  lists it.
- `tua run` behavior is unchanged.

### Phase 2: Conversation and progress

- **Follow-ups.** Ask turns resume the recorded provider session. In code mode, the next prompt
  can continue in the same task worktree until the task is merged or discarded.
- **Progress.** Read Claude Code's `stream-json` events and Codex's `--json` events as they
  arrive, and print one status line per tool action.

**Acceptance:**

- A follow-up turn passes the recorded session ID (fake CLI asserts it).
- A continued code task records several turns and still merges through the unchanged merge
  path.
- Progress lines appear before the run finishes (a fake CLI emits delayed events).
- The final result parsing is identical to today's.
- Cancelling mid-stream keeps the worktree.
- A live probe confirms that `--resume` works headless in the same working directory before
  this phase ships.

### Phase 3: Routing and the task analyzer (rules first)

- **Routes.** Declare routes in the user config: provider, model, effort, billing
  (`subscription` or `api`), and capabilities. A policy maps task classes to ordered routes.
- **Analyzer v1** is deterministic:
  - explicit `/ask`, `/code`, `/research`, `/plan`
  - simple signals: edit verbs, file paths, question form, web hints
- The analyzer shows its choice and reason, and accepts a one-key override.

**Acceptance:**

- Unit tests cover policy resolution.
- On `rate_limited`, the next route of the same billing kind is used (fake CLI).
- An `api` route is never chosen unless `allow_api_billing: true` is set, and its use is
  printed.
- The route, reason, and fallback chain are stored per run.
- No model name appears in code (checked by a test over `src/`).

### Phase 4: Research provenance, inbox, knowledge

- **Structured sources.** Research turns request a structured answer with sources: URL, title,
  quoted passage.
- **Deterministic check.** TUA fetches each source and checks that the quote is there.
- **Inbox and promotion.** `tua inbox` lists findings for Good/Bad. Promotion writes a Markdown
  knowledge note, global or project, after confirmation.
- **Knowledge use.** Knowledge folders are given to later runs as read-only roots.

**Acceptance:**

- Sources are stored per turn.
- Tests against a local HTTP server mark a verified quote as `quote_found`, and a missing one
  as `quote_missing`.
- Fetching refuses non-HTTP schemes and private or loopback addresses, and enforces size and
  time limits.
- No note is written without an explicit approval (test).
- Notes carry their sources and check results.

### Phase 5: Skill lifecycle and the learning loop

- **Managing skills.** Add `tua skill list | show | history | diff`. Source snapshots are stored
  on every compile.
- **Drafting.** `tua skill propose` drafts a patch from selected feedback, in a read-only run.
- **Evaluating.** `tua eval run --proposal <id>` evaluates the patch as an overlay.
- **Approving.** `tua skill approve` applies the patch after showing the comparison.

**Acceptance:**

- A proposal never changes a skill file before approval (test).
- The overlay changes the compiled hash; the live file is untouched.
- Approval is refused if the live file changed since the proposal (hash check).
- Approval without an eval report needs an explicit flag, and that is recorded.
- History lists every compiled and source version used by past tasks.

### Phase 6 (optional): Plans and agent roles

- **Planning.** `tua plan "<goal>"` runs a read-only planner with a JSON schema. The user
  approves or edits the plan.
- **Execution.** Steps run sequentially as ordinary runs: researcher (ask with web), worker
  (code task, chained on the previous step's snapshot), reviewer (peer review).
- **Progress view.** A terminal table shows each step's role, route, state, current activity,
  and reason.

**Acceptance:**

- Plans and steps are persisted.
- Every step is a normal task or turn, linked to the plan.
- Ctrl+C mid-plan keeps all worktrees and records the state.
- The final change merges only through the existing merge checks.
- Nothing runs in parallel in this phase.

### Later, only on request

- **A Gemini adapter** using Gemini CLI with a paid API key. It would be an `api` route, off by
  default.
- **Parallel workers,** once sequential plans have proved useful and quota allows.

## 12. Risks

| Risk | Mitigation |
|---|---|
| **Provider CLI churn.** Flags change, and some are hidden from `--help` (D1). | Keep the zero-cost preflight for every new flag (`--resume`, `stream-json`, schemas), and fail closed. |
| **Terms of use for scripted subscription use.** Google explicitly forbids wrapping `agy` on consumer plans. | Re-check the current Anthropic and OpenAI terms before adding parallel or automated multi-run features. No account rotation, ever. |
| **Quota exhaustion** from plans, reviews, and evals. | Show estimated run counts before starting. Reuse eval-style run and time budgets for plans. Fall back only within the same billing kind. |
| **Ask mode reads live folders,** including untracked secrets. | Secret-file read denies for Claude Code. Label Codex ask turns as able to read anything, as today. |
| **Knowledge poisoning** from web pages or repository text promoted into notes. | Human approval, provenance, and check status on every note. Notes are data, never `must` rules. |
| **Prompt injection through web content** | Already prompt-guided. Keep web opt-in, record it, and never promote unverified content automatically. |
| **Complexity creep** turns a safe tool into a fragile platform | Phases ship separately. No new abstraction without one of the interfaces in the target design. |
| **Schema constraints.** `tasks.worktree_path` and `task_branch` are `NOT NULL UNIQUE` ([db.py:67-68](../src/tua/db.py#L67-L68)), and migrations are additive only. | Store ask turns in a new table rather than forcing them into `tasks`. |
| **Test time grows** (155 s today) | Keep tests on fakes. Mark slow Git-heavy tests if needed. |
| **Merging continued tasks onto a moving base** | Unchanged rule: merge only onto the recorded base. Continuing a task never rebases the user's branch. |

## 13. Open questions

1. Should ask turns share the `tasks` table (with a `mode` column) or get their own `turns`
   table? This audit recommends a new table.
2. Should code mode offer to start from the user's *uncommitted* state, using a snapshot commit
   that is not on their branch, instead of requiring a commit first? Merging back onto a dirty
   checkout would then need new rules.
3. Where should project knowledge live: `.tua/knowledge/` committed with the project, or local
   only like `.tua/` today?
4. What should a bare prompt in `tua` default to: ask, code, or the analyzer's choice?
5. Which routes does the user want by default, and is API billing ever acceptable (for
   example for Gemini)?
6. Is TUA allowed to fetch URLs itself to check quotes, or should checks run as a separate
   provider turn with web access?
7. Does `claude --resume` in headless mode keep working when the permission settings file is
   regenerated for each turn? A live probe is needed.
8. Do the current Anthropic and OpenAI terms permit the planned degree of automation on
   consumer subscriptions?

## 14. Non-goals

- A server, web dashboard, always-on daemon, message broker, or GUI
- A vector database, embeddings, or fine-tuning
- Automatic skill edits, automatic merges, or any push to a remote
- Replacing the providers' own agent runtimes, or building a new agent framework
- Removing worktree isolation for writing tasks in Git projects
- Using Gemini through consumer logins, wrapping Antigravity CLI, or rotating accounts
- Hard-coding model names in code

## Appendix: documentation drift

| Where | Problem |
|---|---|
| [configuration.md:61-63](configuration.md) | Says both profiles always deny `WebFetch` and `WebSearch`. Since D21, they are allowed when a task has web access. |
| [IMPLEMENTATION_PLAN.md](../IMPLEMENTATION_PLAN.md) | "Out of scope … any adapter other than Claude Code" and the status section predate the Codex adapter. |
| [README.md, "Research with sources"](../README.md#several-models-one-workflow) | Implies source handling that does not exist ([section 6.6](#66-research-without-provenance)). |
| [decisions.md](decisions.md) | Says "newest entries go last", but D21 appears before D20 and D19. |
| [CONTRIBUTING.md:17](../CONTRIBUTING.md) | "CI runs the same checks", but the workflow has not been pushed, so CI has never run. |
