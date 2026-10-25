# TUA current capability audit

Date: 2026-10-25. Repository state: `main` at `7974303`, source unchanged; the only
untracked files are `docs/product-gap-audit.md`, `docs/target-architecture.md` and `plan/`.

This audit checks TUA against what it can actually do today, using the code and live runs.
It does not change any source code. The earlier
[product gap audit](../product-gap-audit.md) and the proposed
[target architecture](../target-architecture.md) cover design and roadmap in more depth.
This report adds runtime evidence for providers, usage, web search, and a manual Gemini
workflow.

## 1. Executive summary

- **Two providers work through TUA's real path.**
  - Claude Code: `claude-opus-5-5` by default, and `claude-haiku-5-5` with `--model haiku`.
  - Codex: `gpt-6-luna` on the ChatGPT Go plan.
  - All runs used subscription logins, with no API keys involved.
  - Each smoke test returned `TUA_PROVIDER_TEST_OK` in 3.5 to 5.6 seconds.
- **Gemini is not usable from TUA.**
  - There is no Gemini code in TUA.
  - Gemini CLI rejects your Google AI Pro login.
  - The only automated route would be a paid API key, which was not tested.
  - A manual workflow (section 5) works today with no code changes.
- **Web search works for real, but only when you turn it on.**
  - Both providers searched, opened the PyPI page, and returned the correct answer: ruff
    0.17.2, released 2026-10-22, which I checked independently against PyPI's JSON API.
  - Once web access is on, the AI decides by itself when to search.
  - Sources come back only as free text in the answer. TUA keeps no structured sources,
    dates, access times or checks, and its own records cannot show whether a Claude task
    searched at all.
- **Relative to the vision**, TUA is a solid, safe single-task runner for coding agents.
  - Isolation, permissions, skill hashing, feedback recording and evaluation baselines are
    well built and well tested.
  - Conversation, knowledge, routing, orchestration, source provenance and the feedback
    improvement loop are not built.
  - Every request, including a plain question, still needs a clean Git checkout and a
    worktree.

## 2. Test environment and limitations

| Item | Value |
|---|---|
| OS and tools | Linux 7.0, Python 3.12.3, Git 2.43.0 |
| CLIs | Claude Code 2.1.310, Codex CLI 0.164.0, Gemini CLI 0.65.0. Antigravity `agy` is not installed. |
| Logins (status only, no credentials read) | `claude auth status --json`: `loggedIn: true`, `authMethod: claude.ai`, `subscriptionType: pro`. `codex login status`: "Logged in using ChatGPT". `tua doctor`: both logged in. |
| Isolation | Live tests ran against a throwaway Git repository in the session scratch folder, with `XDG_STATE_HOME` pointed at a scratch state folder. Your real TUA database (`~/.local/state/tua/tua.db`) was only opened read-only. |
| Quota used | 9 small provider tasks between 14:14 and 14:21 UTC: 7 Claude Code, 2 Codex. Claude's reported API-equivalent estimate totals about $0.37, but this is not billed on a subscription. Codex reported 1% used of a 30-day window; that figure is cumulative, not just these tests. |
| Code checks | `PYTHON=.venv/bin/python scripts/dev-check.sh`: formatting and lint passed, strict mypy passed on 28 files, **428 passed, 2 skipped** (the live tests, which spend quota), build passed. |
| Not tested | Gemini with an API key (needs paid billing). Fable models (an earlier session found they need paid credits). Interactive `tua` (needs a terminal; covered only by tests with fake CLIs). Real rate-limit errors (cannot be triggered safely). Interactive web auto-prompt. Live eval runs. |

Note on tests: every test in `tests/unit` and `tests/integration` uses fake provider CLIs
([tests/fixtures/](../../tests/fixtures/)). Only `tests/live/` calls real providers, and it
is skipped unless `TUA_LIVE_CLAUDE=1` or `TUA_LIVE_CODEX=1` is set. Passing tests therefore
prove TUA's own logic, not provider behaviour. The live runs in this report are separate
evidence.

## 3. Providers, models and usage

### 3.1 How each provider is implemented

| Question | Claude Code | Codex | Gemini |
|---|---|---|---|
| Implementation | [adapters/claude_code.py](../../src/tua/adapters/claude_code.py) | [adapters/codex.py](../../src/tua/adapters/codex.py) | None. `grep -ri gemini src/tua` finds nothing. Allowed providers are `("claude-code", "codex")` ([config.py:25](../../src/tua/config.py#L25)). |
| How it is called | Subprocess: `claude --print --output-format json …` ([claude_code.py:198-233](../../src/tua/adapters/claude_code.py#L198-L233)) | Subprocess: `codex exec --json …` | n/a |
| Authentication | The CLI's own login. TUA reads only `loggedIn` and never handles credentials. | The CLI's own login. TUA reads only the leading words of `codex login status`. | n/a |
| Model ID | `--model`, or `default_model` in `.tua/project.yaml`; otherwise the CLI's default. Checked only against a character pattern ([config.py:38](../../src/tua/config.py#L38)), so there is no list of known models. | Same options, passed as `--model`. | n/a |
| Reasoning effort | `--effort` with `low`, `medium`, `high`, `xhigh` or `max` ([config.py:28-31](../../src/tua/config.py#L28-L31)) | `model_reasoning_effort` with `minimal`, `low`, `medium`, `high` or `xhigh` | n/a |
| Streaming | No. Output is collected until the process exits, so nothing shows while it runs. | No | n/a |
| Timeout and cancel | Yes. The whole process group is killed on wall-clock timeout or Ctrl+C. | Yes | n/a |
| Retries and fallback | None in TUA, for either provider | None | n/a |
| Error types | Login errors and rate limits are recognized from the API status and from text patterns ([claude_code.py:43-49](../../src/tua/adapters/claude_code.py#L43-L49)). Unknown models and network failures are **not** told apart (see 3.3). | Login errors and rate limits are recognized from text patterns ([codex.py:64-67](../../src/tua/adapters/codex.py#L64-L67)). | n/a |
| Where results are stored | SQLite `tasks` table: `actual_models`, `cost_usd`, `usage_json`, `num_turns`, `session_id`, `error_type` ([db.py:58-97](../../src/tua/db.py#L58)). Raw output and `result.md` go in `~/.local/state/tua/tasks/<id>/`. | Same table, but `actual_models` is always empty and `cost_usd` is null. Events go in `codex-events.jsonl`. | n/a |
| Checking status and model | `tua doctor` (install, flags, login), `tua status <id>` (reported models, effort, cost, session), `tua project show` | Same | n/a |
| Task types | Any prompt, but every task is a Git worktree task. Profiles are `safe` and `read-only`, `tua review` is a read-only review task, and `--web` adds web tools. | Same, except command allowlists are not enforced and the sandbox can read files outside the worktree ([codex.py:7-14](../../src/tua/adapters/codex.py#L7-L14)). | n/a |

### 3.2 Smoke tests through `tua run`

All runs used `tua run --project audit-smoke --profile read-only` with the prompt *"Reply
with exactly TUA_PROVIDER_TEST_OK and nothing else. Do not use any tools."*

| Provider/runtime | Model ID (observed) | Invocation method | Authentication status | Smoke test | Observed usage | Limitations/risks |
|---|---|---|---|---|---|---|
| Claude Code via TUA, CLI default | `claude-opus-5-5` (from Claude's `modelUsage`) | CLI subprocess with `--effort low` | claude.ai OAuth, Pro | **PASS**: `TUA_PROVIDER_TEST_OK` in 4.1 s, 1 turn (task `20261025-141428-bef854`) | 2 input + 19 output tokens, 10,368 cache-write and 8,658 cache-read tokens; API-equivalent estimate $0.0851, not billed | The default is the most expensive model. Remaining plan quota is not reported anywhere in Claude's JSON output. |
| Claude Code via TUA, `--model haiku` | `claude-haiku-5-5` | CLI subprocess | Same | **PASS** in 3.5 s (`20261025-141448-3fa41f`) | Estimate $0.0023 | None seen |
| Claude Code via TUA, `--model sonnet` | `claude-sonnet-5-5` | CLI subprocess | Same | **PASS** in your real database: 2 merged tasks, earlier on 2026-10-25 | Estimates $0.07 and $0.11 | Not re-run today |
| Claude Code, unknown model `tua-nonexistent-model-x1` | none | CLI subprocess | Same | **PASS** for error handling: failed in 2.4 s with Claude's message *"There's an issue with the selected model …"* | 0 tokens | Recorded as the generic `provider_error`, not as an unsupported model |
| Codex via TUA, CLI default | `gpt-6-luna` (from Codex's own session log, field `model`; `plan_type: go`) | CLI subprocess with `--effort low` | ChatGPT OAuth, Go plan | **PASS**: `TUA_PROVIDER_TEST_OK` in 5.6 s (`20261025-141459-6412dc`) | 11,858 input and 9 output tokens. Rate limit: `used_percent: 1.0` of a 43,200-minute window, from Codex's session log only. | **TUA records no model for Codex**, because `codex exec --json` never reports it. An earlier session found the default (`gpt-6.1-sol`) was refused on Go; today the default resolved to `gpt-6-luna`. Treat the default as plan-dependent and changeable. |
| Gemini CLI 0.65.0, Google login (`oauth-personal`) | none | CLI, outside TUA | Google AI Pro account | **FAIL** (seen earlier on 2026-10-25): `IneligibleTierError`, `UNSUPPORTED_CLIENT` | none | Google stopped serving personal, Pro and Ultra accounts in Gemini CLI on June 18, 2026 ([announcement](https://developers.googleblog.com/an-important-update-transitioning-gemini-cli-to-antigravity-cli/)). |
| Gemini via paid API key or Vertex AI | none | would be the CLI or the API | none configured | **NOT VERIFIED**: needs pay-as-you-go billing, separate from your Pro plan | none | Not part of TUA (**NOT IMPLEMENTED**) |
| Antigravity `agy` | none | none | none | **NOT IMPLEMENTED**, not installed | none | Antigravity's FAQ does not support wrapping it in third-party apps on consumer plans. Do not add it. |
| Gemini web app or Gemini project | your choice in the app | manual, outside TUA | your Google AI Pro subscription | not applicable (manual) | Shown in the Gemini app only | See section 5 |

### 3.3 Subscriptions versus API billing

- **Claude Pro** reaches TUA through Claude Code's claude.ai login.
  - TUA shows Claude's `total_cost_usd` as an "API-equivalent estimate … (subscriptions use
    plan quota and are not billed per token)", which is accurate labelling.
  - Remaining quota and reset times are not in Claude's JSON output, so TUA cannot show them.
- **ChatGPT Go** reaches TUA through Codex CLI's ChatGPT login.
  - Codex records rate-limit percentages and reset times in `~/.codex/sessions/…`, but not in
    the `--json` events TUA reads, so TUA shows neither.
- **Google AI Pro** has no supported automated route.
  - Gemini CLI rejects it, and wrapping Antigravity is ruled out.
  - The Gemini API is billed separately from the subscription.
- **No API keys** are used, stored or needed by TUA. Its log scrubbing removes key-shaped
  strings ([logging_utils.py:35-36](../../src/tua/logging_utils.py#L35-L36)).

## 4. Product-goal alignment

| Product goal | Status | Repository evidence | Remaining gaps | Priority |
|---|---|---|---|---|
| Simple terminal experience, close to Claude Code | PARTIALLY IMPLEMENTED | Interactive `tua` registers the folder, reads prompts (`\` for more lines, `@file`, `/web`), and offers merge or discard ([interactive.py](../../src/tua/interactive.py)) | Each prompt is a separate task with no follow-up. Nothing shows until the provider finishes ([process.py](../../src/tua/process.py) collects output until exit). | P1 |
| Ask questions, research and read documents without changing code | PARTIALLY IMPLEMENTED | The `read-only` profile and `--web` both work (live, today) | Still needs a clean checkout and creates a worktree and branch ([tasks.py:196](../../src/tua/tasks.py#L196)). It answers from the last commit, not your current files. Interactive mode offers `git add -A` to clean up ([interactive.py:405-411](../../src/tua/interactive.py#L405-L411)). | P1 |
| Start, monitor, review, accept or reject tasks | IMPLEMENTED & VERIFIED | `run`, `status`, `diff`, `merge`, `discard`; live runs today; lifecycle tests | No live progress view (`status` shows only the state) | P1 (progress) |
| Pick up where you left off | PARTIALLY IMPLEMENTED | Projects, tasks and worktrees persist across restarts; `status` prints a `claude --resume <session>` command | TUA cannot continue a task or conversation itself, and has nothing for Codex | P1 |
| Multi-agent orchestration (planner, specialists, cross-checking) | NOT IMPLEMENTED (a single reviewer exists) | `tua review` runs one recorded read-only review task ([review.py](../../src/tua/review.py), [peer_review.py](../../src/tua/peer_review.py)) | No planner, subtasks, dependencies, roles, or decision about when several agents help | P2 |
| Automatic model routing | NOT IMPLEMENTED | Only manual `--provider`, `--model`, `--effort` and project defaults (manual selection **VERIFIED** today) | No complexity analysis, no rules, no fallback when a provider is out of quota or refuses a model | P1 |
| Recording outcomes for model evaluation | PARTIALLY IMPLEMENTED | Claude: models, tokens, cost estimate, turns, duration and error type are stored. Eval suites with pinned baselines exist ([evaluations.py](../../src/tua/evaluations.py)). | Codex model never recorded. No quota or latency summaries. **0 eval runs** in your database: evals are tested with fakes only. | P0 (Codex model), P1 |
| Different models for planning, execution, review and research | PARTIALLY IMPLEMENTED | A review can use a different provider or model from the task it reviews | No planning or research roles | P2 |
| Global and project skills, with mandatory-rule precedence | IMPLEMENTED BUT NOT VERIFIED | `collect_sources`: built-in rules, then global, then project ([skills.py:144](../../src/tua/skills.py#L144)). A `must` rule can never be overridden ([skills.py:263](../../src/tua/skills.py#L263)). Unit-tested. | No global config exists on this machine (`~/.config/tua` is absent), and no end-to-end test runs a global skill through a real task. Global skills are added only by editing YAML. | P0 (test) |
| Detecting conflicting or duplicated instructions | PARTIALLY IMPLEMENTED | Duplicate rule IDs and attempts to redefine `must` rules are errors ([skills.py:16-19](../../src/tua/skills.py#L16-L19)) | Only exact ID clashes are caught; two rules that contradict each other under different IDs are not | P2 |
| Loading only the skills a task needs | NOT IMPLEMENTED | Every configured skill is compiled into every task | No selection by task type or relevance | P2 |
| Project knowledge, documents and decisions | DESIGNED ONLY | Proposed in [target-architecture.md §5](../target-architecture.md) (not reviewed) | TUA stores task records only; no knowledge store, index or search | P1 |
| Approved knowledge kept apart from unverified material | DESIGNED ONLY | Inbox and knowledge notes proposed in the target architecture | None | P1 |
| Domain workflows (papers, lab experiments) | NOT IMPLEMENTED | none | Everything runs as a coding task | P2 |
| Recording feedback with task, model and skill version | IMPLEMENTED & VERIFIED | `tua good` and `tua bad` store fingerprinted rows (2 rows in your database); tests | none | none |
| Feedback → cause analysis → candidate → eval → approval → versioned rollback | NOT IMPLEMENTED (only step 1 exists, and step 4 partly) | Nothing outside `feedback.py`, `db.py` and `cli.py` reads feedback. Evals can compare skill files on disk with a baseline. | No analysis, no proposals, no approval step, no stored skill history to roll back to | P1 |
| Web search and collecting sources | IMPLEMENTED & VERIFIED (opt-in) | Section 6: live search and page open with both providers | Sources exist only as free text in the answer | none for search; P1 for sources |
| Source metadata, facts versus inferences, cross-checking | NOT IMPLEMENTED | none | No stored titles, URLs, dates or access times; no check that quotes match the page; no rule separating sourced facts from inference | P1 |
| Rating sources, and keeping unverified results out of knowledge | NOT IMPLEMENTED | none | No inbox or source ratings, and no knowledge store to protect | P1 |
| Manual Gemini step | NOT IMPLEMENTED (a workaround exists) | Section 5 | No "waiting for you" state, no hand-off view, no import command | P2 |

### 4.1 What TUA can do today

TUA can run one Claude Code or Codex task at a time in an isolated Git worktree:
- with a fixed permission plan, plus optional read-only access to the folder you started
  in and optional web access
- record what ran: provider, reported model (Claude only), skill hashes, cost estimate and
  tokens
- let you diff, merge or discard the result safely
- have a second model review it
- record good or bad feedback
- run evaluation suites against pinned baselines

### 4.2 Strong foundations to keep

- **Task lifecycle.** The state machine, crash recovery, and timeout and Ctrl+C handling
  (`tasks.py`, `process.py`).
- **Merging.** Worktree isolation and the squash-merge safety checks (`worktrees.py`,
  `review.py`).
- **Permission plans.** `--setting-sources user`, `--strict-mcp-config`, and web tools
  denied unless allowed ([permissions.py:107-109](../../src/tua/permissions.py#L107-L109)).
- **Adapter contract.** The typed request and result in
  [adapters/base.py](../../src/tua/adapters/base.py) never guesses unknown values, and
  zero-cost preflight checks run before each task.
- **Skill compiler.** Precedence rules plus source and compiled hashes.
- **Database.** Append-only events and feedback.
- **Evaluations.** Eval baselines.
- **Tests.** 428 passing tests with realistic fake CLIs.

### 4.3 Where the implementation does not match the vision

1. **Everything is treated as a coding task.**
   - Questions, research and document reading all need a clean checkout, a branch and a
     worktree, and they see the last commit instead of your current files.
   - This is the biggest gap between the code and the vision.
2. **Every task stands alone.**
   - There is no conversation, follow-up, or project memory.
   - What you learned in one task never reaches the next.
3. **Research gives answers, not evidence.**
   - Search works, but sources are not kept as records and claims are not checked.
4. **Feedback goes nowhere.** It is recorded and never read.
5. **Routing is entirely manual.**
   - Model choice falls back to each CLI's default, which is the most expensive Claude model
     and a plan-dependent Codex model.

### 4.4 What to address first

1. **Make results checkable** (P0 below). Otherwise you cannot tell which Codex model ran,
   whether a Claude task searched, or why a task failed without a network.
2. **Questions and research without a worktree** (P1). This covers most of the day-to-day
   use in the vision.
3. **Follow-ups and live progress** (P1). These are the main usability gaps, and both CLIs
   already support resuming sessions and streaming events.
4. **Sources and knowledge, then the feedback improvement loop** (P1). They depend on items
   2 and 3.

## 5. Manual Gemini workflow

### 5.1 What TUA can do now

| Question | Answer | Evidence |
|---|---|---|
| Can TUA ask you to do a manual step? | **No.** Task states are only `created`, `preparing`, `running`, `completed`, `failed`, `timeout`, `cancelled`, `merged` and `discarded`; there is no "waiting for you" state. | [models.py:15-25](../../src/tua/models.py#L15-L25) |
| Can it show a full prompt and a file list? | **Only as ordinary AI output.** If you ask a task to write a Gemini prompt and list the files to upload, the answer appears on screen and in `result.md`. TUA does not check that the paths exist and has no special format for this. | `result.md` is written for every task (`AdapterResult.result_text`) |
| Can the task resume when you bring the answer back? | **No.** You start a new task with Gemini's answer as its prompt: in interactive `tua`, type `@/path/to/file` (up to 100,000 characters, and prompts that look like secrets are refused), or pass the text to `tua run`. | [interactive.py:321-331](../../src/tua/interactive.py#L321), [tasks.py:59](../../src/tua/tasks.py#L59) |
| Does TUA ever call Gemini by itself? | **No.** There is no Gemini code, and nothing opens a browser. | `grep -ri gemini src/tua` finds nothing |

**Minimal pieces to add later** (not built in this audit):
1. A hand-off result: a "manual step" output naming the reason, the prompt, and the files,
   each checked to exist, with its absolute path and the reason it is needed.
2. A `tua import <task-id> <file>` command, or an interactive `/paste`, that attaches the
   returned text as untrusted input to a follow-up of the same task.
3. A record that the hand-off happened and which file hashes were uploaded.

None of these need a browser, an API, or a new provider adapter.

### 5.2 Worked example you can use today

Task: get an independent review of the two new audit documents against your original
vision, from a different model family than the one that wrote them.

**Why Gemini**

Claude wrote both audit documents and also produced most of TUA's code. A reviewer from a
different model family, working only from the documents you upload, is less likely to share
the same blind spots. It also tests whether the documents are understandable without
access to the code.

**Prompt to copy**

```text
You are reviewing the design documents of TUA, a command-line tool that runs AI coding
agents (Claude Code, Codex) on tasks in isolated Git worktrees. I have uploaded three files:

1. TUA_Architecture_and_Claude_Code_Prompt.md: my original product vision and plan.
2. product-gap-audit.md: an audit of what the current code does and does not do.
3. target-architecture.md: a proposed design for closing the gaps.

You cannot see the source code. Work only from these files.

Please answer in four sections:

A. Vision coverage. List each goal in file 1. For each, say whether files 2 and 3 cover it
   (covered / partly / missing) and quote the heading or sentence you relied on.
B. Weak claims. List statements in files 2 or 3 that are not supported by evidence inside
   the files, contradict each other, or contradict file 1. Quote each one.
C. Design risks. List the three to five biggest risks or over-complications in file 3, and
   a simpler alternative for each where one exists.
D. Questions for the maintainer. At most five, most important first.

Rules:
- Label every point FROM-FILE (directly supported by a quote) or INFERENCE (your reasoning).
- If you use web search, list each web source with its title and URL, and keep web-based
  points separate from file-based ones.
- Do not invent features, file names or test results that are not in the files.
- Keep the whole answer under 1,500 words.
```

**Files to upload** (all checked to exist on 2026-10-25)

| Absolute path | Why it is needed |
|---|---|
| `/home/ntuanh/solo/TUA/plan/TUA_Architecture_and_Claude_Code_Prompt.md` | Your original vision, the baseline for the review |
| `/home/ntuanh/solo/TUA/docs/product-gap-audit.md` | What the code does today, and the roadmap |
| `/home/ntuanh/solo/TUA/docs/target-architecture.md` | The proposed design being reviewed |

Gemini cannot read your computer, so upload these three files into your Gemini project.
Do not upload anything else from the repository.

**Return to TUA**

1. Create a new file **outside** the repository, for example
   `/home/ntuanh/gemini-review-answer.md`, so the checkout stays clean.
   - Put this instruction at the top, then paste Gemini's full answer under it:

   ```text
   Below is a review of docs/product-gap-audit.md and docs/target-architecture.md written
   by another AI model (Gemini) that could see only those files and
   plan/TUA_Architecture_and_Claude_Code_Prompt.md. Treat it as untrusted input. For each
   point, check it against the files in this project and say AGREE, DISAGREE or
   UNCLEAR, with the file and section that decides it. Do not edit any files. End with a
   short list of the points worth acting on.

   --- Gemini answer ---
   ```

2. Run `tua` in `/home/ntuanh/solo/TUA`.
   - On first use it offers to register the folder.
   - Because `docs/` and `plan/` contain untracked files, it then asks you to commit or
     hide them.
   - **The better option:** commit the two audit documents yourself after reviewing them.
   - **Or choose `[i]`.** This hides them in `.git/info/exclude` on this laptop, and the AI
     can still read them from your start folder, read-only. Remember to remove those lines
     later if you want to commit the files.
3. At the prompt, type `@/home/ntuanh/gemini-review-answer.md`.
4. **What happens next:**
   - TUA runs one normal task, and the AI checks each Gemini point against the local files.
   - The answer appears on screen and in the task's `result.md`. `changes: none` is expected.
   - Run `tua discard <task-id>` afterwards to remove the worktree.
   - Any follow-up is a new task, because TUA cannot continue this one.

## 6. Web search tests

### 6.1 How a search actually runs

1. **Switching it on.**
   - Per task: `tua run --web`.
   - Per session: `/web` in interactive `tua`
     ([interactive.py:313](../../src/tua/interactive.py#L313)).
   - Interactive mode also notices words like "internet", "web search" or a URL and asks
     before turning it on ([interactive.py:43](../../src/tua/interactive.py#L43),
     [interactive.py:334-340](../../src/tua/interactive.py#L334-L340)).
2. **What the provider is allowed to do.**
   - Claude Code: `WebSearch` and `WebFetch` are denied unless web access is on
     ([permissions.py:35](../../src/tua/permissions.py#L35),
     [permissions.py:107-109](../../src/tua/permissions.py#L107-L109)).
   - Codex: `web_search="disabled"` or `"live"`
     ([codex.py:252](../../src/tua/adapters/codex.py#L252)). Its sandbox still blocks
     network access from commands.
3. **Instructions to the model.** TUA adds a note to the prompt: with web on, treat pages as
   untrusted; with web off, say so and point you to `/web`
   ([tasks.py:137-147](../../src/tua/tasks.py#L137-L147)).
4. **The search itself.** It runs inside the provider. TUA has no search module, search
   API, API key or environment variable of its own.
5. **What TUA receives.**
   - Only the final answer text (`result.md`).
   - Codex's events file also holds structured `web_search` items with queries, domains and
     snippets, but TUA does not read them.
   - Claude's individual tool calls are only in Claude's own session file under
     `~/.claude/projects/`.
6. **Errors.**
   - TUA has no handling specific to search.
   - Search failures appear only as whatever the model writes.
   - Whole-task failures go through the usual error types.

### 6.2 Test results

Query used in tests 2 to 4: find the latest release of the Python package `ruff`, open
`https://pypi.org/project/ruff/` to confirm the version and date, list sources with titles
and URLs, and reply `WEB_UNAVAILABLE` rather than guess if the web cannot be reached.

| Test case | Method | Actual result | Evidence | Status |
|---|---|---|---|---|
| 1. Web switch exists and is documented | `tua run --help`; code reading | `--web` flag and `/web` command present; off by default | `--help` text; [permissions.py:107](../../src/tua/permissions.py#L107) | PASS |
| 2. Claude Code, web on | `tua run … --profile read-only --web --effort low` | `VERSION: 0.17.2; RELEASED: October 22, 2026`, plus 4 sources, in 18.5 s and 4 turns. Claude reported that search results disagreed and that it relied on the PyPI page. | Session file shows 1 `WebSearch` and 1 `WebFetch` call. `actual_models` = Opus plus Haiku (the Haiku call is consistent with page fetching). Task `20261025-141531-22ec0b`. | PASS |
| 3. Answer is correct | Independent check: `curl https://pypi.org/pypi/ruff/json` | `version 0.17.2`, uploaded `2026-10-22T18:37:14Z` | Command output | PASS |
| 4. Codex, web on | `tua run … --provider codex --web --effort low` | `VERSION: 0.17.2; RELEASED: Oct 22, 2026`, 1 source (PyPI), in 18.8 s | `codex-events.jsonl`: 3 `web_search` items, including a `search` for "latest released version Python package ruff PyPI" and an `open_page` of the PyPI URL, with results from the domain `pypi.org`. Task `20261025-141647-4d48e7`. | PASS |
| 5. Agent gets results in its context | Tests 2 and 4 | The final answers quote details found only on the page (version and date) | As above | PASS |
| 6. Sources and metadata kept | Read `result.md` and the database | Titles and URLs appear only inside the answer text, and only because the prompt asked for them. No structured source records, publication dates or access times. Claude listed two unrelated older pages as sources (newreleases.io 0.11.9, LCG 0.15.14). | `result.md`; no source table in [db.py](../../src/tua/db.py) | NOT IMPLEMENTED |
| 7. TUA can tell whether a search happened | Read the stored `usage_json` for test 2 | `server_tool_use: {web_search_requests: 0, web_fetch_requests: 0}` even though both tools ran | Database row for `20261025-141531-22ec0b` | FAIL (not visible in TUA's records) |
| 8. Web off, prompt says "do not guess" | Same query without `--web` | `WEB_UNAVAILABLE`, explains web access is off, suggests `/web` or `--web` | Task `20261025-141707-378f12` | PASS |
| 9. Web off, plain question | "What is the latest released version of the Python package ruff, and when was it released?" | Declined to give a version from memory, explained why, and suggested `/web` | Task `20261025-141722-e395c9` | PASS (model behaviour guided by TUA's prompt note, not enforced) |
| 10. Network failure | `HTTPS_PROXY=http://127.0.0.1:9` for one run, `--timeout 90` | Reported `timeout: wall-clock timeout exceeded` after 90.2 s. Stdout and stderr were both empty, with no hint that the network was the cause. | Task `20261025-141952-13e32c` | PARTIAL (fails safely, misleading message) |
| 11. Interactive auto-prompt for web | Not run (needs a terminal) | Not observed live | Covered by fake-CLI tests in [test_web_access.py](../../tests/integration/test_web_access.py) | NOT VERIFIED |

### 6.3 Answers

- **How much works:** live search and page opening through both providers, opt-in, with
  correct results in this test. Everything after the answer is missing: storing sources,
  dates and access times, and checking or rating sources.
- **Does TUA start searches itself:** you must turn web access on, per task or per session.
  After that the AI decides when to search, so this is **AUTO once you allow it**. With web
  off, the provider has no search tools at all.
- **Do results reach the agent:** yes (tests 2, 4 and 5).
- **Are citations and metadata kept:** only as free text in the answer. Claude's source list
  included pages that do not support the answer, and nothing checks this.
- **What blocks more automation:** no provider-side problem. TUA would need its own source
  records and checks (section 8). Making search automatic for every task is a deliberate
  security choice, not a gap: web pages can try to mislead the AI and can be used to leak
  data ([docs/security.md](../security.md)).

## 7. Confirmed problems versus unverified hypotheses

**Confirmed by observation today**

1. TUA records no model for Codex tasks (`actual_models` is null). The model is only in
   Codex's own session log.
2. TUA's stored usage cannot show whether a Claude task searched the web (test 7).
3. A network failure shows up as a plain timeout after the full timeout period, with no
   cause given (test 10).
4. An unknown model is recorded as `provider_error`, although Claude's message is passed on
   clearly.
5. Remaining quota is not available from Claude's output. Codex's quota is only in its
   session log.
6. Questions need a clean checkout and a worktree. Code reading confirms it: `ensure_clean`
   runs for every profile ([tasks.py:196](../../src/tua/tasks.py#L196)). Today's runs created
   a worktree even for a read-only question.
7. Nothing reads stored feedback (`grep` outside `feedback.py`, `db.py` and `cli.py`).
8. The CI workflow commit `7974303` has not been pushed, so CI has never run on GitHub.

**Not verified (hypotheses)**

1. **Read-only Claude tasks (including every `tua review`) cannot list or search files.**
   - The shell is denied ([permissions.py:122](../../src/tua/permissions.py#L122)), and an
     earlier live test found that this Claude Code version searches through the shell.
   - Not re-tested today; my read-only smoke tests used no tools.
2. **Claude Code keeps retrying silently** when the network is down, which would explain the
   empty logs in test 10. This is inferred, not observed.
3. **`server_tool_use` stays at zero** because Claude Code runs WebSearch and WebFetch on the
   client side rather than as server tools. This is inferred.
4. **Codex's default model depends on the plan** and can change without notice. This is
   inferred from the earlier session (default refused) versus today (default worked).
5. **A paid Gemini API key would work** through Gemini CLI in headless mode. This is
   untested and would cost money.

## 8. Prioritized gaps

**P0: stops you from using or checking a core capability**

- Record the Codex model. It is in Codex's session log, keyed by the `thread_id` that TUA
  already receives, or Codex could be pinned with an explicit `--model`.
- Record whether web tools ran. Both are available today: Claude Code's
  `--output-format stream-json` events, and Codex's `web_search` items, which TUA already
  saves.
- Tell a network failure apart from a timeout, so that a task does not wait out the full
  timeout without saying why.
- Run one live check that read-only Claude tasks can list files (affects `tua review`).
- Add one end-to-end test of a global skill.
- Get CI running: grant the `workflow` scope, then push.

**P1: needed soon for the vision**

- Questions, research and document reading without a worktree (ask mode in the target
  architecture).
- Follow-ups and live progress.
- Rule-based routing with fallback, never spending API money unless you opt in.
- Source records (URL, title, date, access time) and a quote check.
- An inbox for unverified material, and project knowledge notes.
- The feedback loop: analysis, proposal, eval against baseline, your approval, versioned
  rollback.

**P2: later**

- Planner, subtasks and agent roles.
- Context-aware skill loading.
- Detecting rules that contradict each other.
- Domain workflows (papers, lab).
- A manual hand-off step with an import command (5.1).
- A Gemini API route, if you ever accept per-request billing.

## 9. Recommended next actions (not implemented)

1. **Decide whether to commit** `docs/product-gap-audit.md`, `docs/target-architecture.md`
   and this report after reviewing them. They are untracked, and they make every TUA task
   in this repository refuse to start until they are committed or hidden.
2. **Push the CI commit yourself:** `gh auth refresh -h github.com -s workflow`, then
   `git push origin main`.
3. **Try the Gemini workflow in section 5.2.** The review it produces is a useful check on
   the roadmap before any building starts.
4. **When you are ready to build, start with the P0 items.** They are small and make every
   later result verifiable. Then move to ask mode.
5. **Pin `default_model` per project** if you want predictable models and quota use. Today
   both providers fall back to whatever their CLI picks.
