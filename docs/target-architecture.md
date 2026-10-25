# Target architecture

**Status:** proposal for review. Nothing here is implemented. The design builds on the current
code and removes none of its safeguards. The reasons and the phased plan are in
[product-gap-audit.md](product-gap-audit.md).

## 1. Principles

1. **Reuse the providers' agents.** Claude Code and Codex do the agent work. TUA chooses,
   isolates, records, and asks for approval.
2. **Direct path first.** A simple question is one provider run with no Git work. Planning and
   multiple roles are opt-in.
3. **Isolation proportional to risk.** A Git worktree is used whenever a run may write to a
   project, and only then.
4. **Local and inspectable.** State lives in SQLite and plain files under the XDG directories.
   There is no server, daemon, dashboard, or vector database.
5. **People approve lasting changes.** Merges, file writes outside TUA's folders, knowledge
   notes, and skill edits all need an explicit yes.
6. **Billing is explicit.** Subscription CLI use and per-request API billing are separate route
   kinds. API billing is off unless the user turns it on.
7. **No model names in code.** Routes come from the user's configuration and are checked by the
   existing zero-cost preflight.
8. **No new abstraction without a need.** Section 3 lists the four interfaces this design needs.
   Planner and agent roles are compositions of them, not a framework.

## 2. Overview

```text
 tua  (session)          tua ask · run · plan · inbox · skill · status  (scriptable)
   │                                   │
   └──────────────► Session manager ◄──┘   conversations and turns (SQLite)
                          │
                    Task analyzer          mode + task class + reason, shown and overridable
                          │
        ┌─────────────────┼──────────────────┬────────────────────────┐
     Ask mode       Knowledge mode       Code mode               Plan (optional)
   live folder or   staging folder,      Git worktree:           steps = ordinary
   scratch,         explicit apply       today's task core       ask / code / review runs
   read-only                                                        + progress view
        └─────────────────┴────────┬─────────┴────────────────────────┘
                                   │
                            Provider router         routes from config; billing gate; fallback
                                   │
                ProviderRun: compile skills → permission plan → preflight → run → record
                                   │
                  adapters: claude-code · codex · (gemini-api: opt-in, API-billed)
                                   │
   SQLite: tasks, turns, plans, sources, inbox, skill versions, proposals
   Files:  skills and knowledge (global + project) · task, turn, and plan artifacts (XDG state)
```

## 3. Minimal interfaces

These four interfaces are all the design needs. Each one generalizes code that exists today.

### 3.1 `Workspace`: where a run executes

```python
@dataclass(frozen=True)
class Workspace:
    kind: Literal["live", "scratch", "staging", "worktree"]
    cwd: Path                      # the provider's working directory
    read_roots: tuple[Path, ...]   # extra read-only folders: start folder, library, knowledge
    write_root: Path | None        # None: read-only. Otherwise the only writable folder.
```

| Kind | `cwd` | `write_root` | Used by |
|---|---|---|---|
| `live` | the user's folder (Git or not) | `None` | ask mode |
| `scratch` | `state/turns/<id>/` | `None` | ask mode started outside any folder of interest |
| `staging` | `state/turns/<id>/out/` | that folder | knowledge mode |
| `worktree` | `state/worktrees/<task-id>/` | that worktree | code mode (today's behavior) |

`permissions.build_plan` already takes a working path and context roots. It needs three changes:

- treat `write_root` as the only `Edit` allow
- add secret-file `Read` denies for `live` workspaces
- pass `read_roots` as `--add-dir`

For Codex, `write_root is None` maps to `--sandbox read-only` and anything else to
`workspace-write`.

### 3.2 `ProviderRun`: one provider invocation

This is the "prepare, preflight, run, record" block of `TaskService._execute`
([tasks.py:299](../src/tua/tasks.py#L299)), extracted so that both code tasks and turns use it:

```python
def run_provider(workspace: Workspace, route: Route, prompt: str, *,
                 profile: str, web: bool, resume_session: str | None = None,
                 output_schema: Path | None = None,
                 on_event: Callable[[ProviderEvent], None] | None = None) -> AdapterResult: ...
```

`AdapterRequest` gains:

- a working directory (code mode still passes the worktree)
- `resume_session`
- `output_schema`
- `on_event`

Each new flag goes through the existing preflight, which runs the exact argv with empty stdin.
The installed CLIs already offer what this needs (checked in `--help`):

| Need | Claude Code 2.1.310 | Codex 0.164.0 |
|---|---|---|
| Follow-up | `--resume <id>`, `--continue` | `codex exec resume <id>` |
| Structured output | `--json-schema` | `--output-schema <file>` |
| Streamed events | `--output-format stream-json` | `--json` (already used) |

### 3.3 `Route`: provider, model, effort, and billing

```python
@dataclass(frozen=True)
class Route:
    name: str                          # user-chosen, e.g. "quick"
    provider: str                      # "claude-code" | "codex" | later "gemini-api"
    model: str | None                  # copied from user config; never a code constant
    effort: str | None                 # validated per provider, as today
    billing: Literal["subscription", "api"]
    capabilities: frozenset[str]       # e.g. {"edit", "web", "resume", "schema", "stream"}
```

### 3.4 `Conversation` and `Turn`: the session record

```python
@dataclass(frozen=True)
class Turn:
    id: str
    conversation_id: str
    seq: int
    mode: Literal["ask", "knowledge", "code"]
    workspace_kind: str
    cwd: str
    task_id: str | None        # set for code turns; links to the existing task record
    route_name: str
    route_reason: str
    provider_session_id: str | None
    web: bool
    status: str                # same terminal states as a task run
```

**Planner and roles are not new types.** A role is a combination of a mode, a permission
profile, a prompt template, and a task class for the router. The plan executor is a loop over
`run_provider` and `TaskService.run`.

## 4. Global personal core and skills

```text
~/.config/tua/
  config.yaml        skills, library folders, routes, policy, interactive settings
  skills/*.md        global skills (same format as today)
  knowledge/*.md     global knowledge notes (approved only)
~/.local/state/tua/
  skills/<sha>.md    every source skill version ever compiled (new)
  compiled/<sha>.md  every compiled payload (exists today)
```

- **Skill format and precedence stay exactly as today.** The order is:
  1. built-in `must`
  2. global `must`
  3. project `must`
  4. project `default`
  5. global `default`
  6. task prompt

  The existing conflict rules apply: no redefined `must`, no class changes, no duplicates
  within a level.
- **Version history.** Each compile stores the source bytes by hash (`skill_versions` table and
  `state/skills/`). This gives a history even when `.tua/` is not committed. The commands
  `tua skill list | show | history | diff` read it. `tua skill list` also shows overrides,
  meaning which project `default` shadows which global one. `tua project show` lists that
  today.
- **Personal documents ("library").** In `config.yaml`, set `library: [~/notes, ~/papers]`.
  These folders are added as read-only roots for every run, validated with the same rules as
  today's context folder: never the home folder itself, a top-level folder, or a folder
  containing TUA's state. They are never copied or indexed into a database.
- **Global knowledge.** Approved notes (section 9) live in `knowledge/`. They are offered to
  runs as a read-only root, with a short index line in the prompt notes.

## 5. Project-local layer

```text
<project>/.tua/
  project.yaml       existing keys, plus optional route-policy overrides and knowledge settings
  skills/*.md        project skills (unchanged)
  knowledge/*.md     approved project notes, including "memory" notes (new)
  evals/*.yaml       eval suites (unchanged)
```

- **The config fingerprint still guards `project.yaml`.** New keys, such as route overrides,
  are covered by the same re-approval. A project config may *restrict* routes and billing
  (for example `allow_api_billing: false`) but cannot enable API billing that the user config
  does not allow.
- **Memory is knowledge, not a separate system.** A memory note is a knowledge note with
  `kind: memory`:
  - When the user types `/remember …`, TUA writes the note after a confirmation.
  - When the model suggests remembering something, the suggestion becomes an inbox item. It is
    never written directly.
- **Feedback** stays in SQLite (`feedback` table, unchanged). It gains read paths:
  - `tua feedback list --project P --skill-hash H`
  - grouping by compiled skill hash
- **Commit policy** for `.tua/knowledge/` follows the project's choice for `.tua/` (open
  question 3 in the audit).

## 6. Session manager and task analyzer

### Session manager

- **Conversations.** A `tua` session is one conversation; `tua --continue` reopens the last one
  for this folder. Each request is a turn.
- **Ask turns** resume the provider session recorded on the previous ask turn of the same
  conversation and provider. The working directory stays the same, because Claude Code keys
  sessions by directory.
- **Code turns.** A code request starts a task, exactly as today. Until that task is merged or
  discarded, the next code request may **continue** it in the same worktree with
  `--resume <session>`:
  - The menu gains `[f] follow up`.
  - This adds one transition to the state machine: an explicit `continue` operation moves a
    retained task (`completed`, `failed`, `timeout`, `cancelled`) back to `running`. It is
    recorded as an event with the turn number.
  - Merge and discard rules do not change.
  - This change needs review before Phase 2.
- **Progress.** `on_event` turns provider events into single lines ("reading
  src/export.py", "running python -m pytest", "searching the web") printed while the run
  works. Events are also appended to the turn's log for `tua status`.

### Task analyzer (rules first)

| Signal | Mode / class |
|---|---|
| `/ask`, `/code`, `/research`, `/plan`, `/doc` typed by the user | That mode. This always wins. |
| An open code task in this session plus a follow-up wording ("also", "now", "fix that") | code (continue) |
| Edit verbs ("add", "fix", "refactor", "rename", "implement") plus a file, symbol, or test mention, inside a Git project | code-small, or code-large when it mentions several areas or is long |
| A question form ("how", "why", "what", "explain", "where"), with no edit verb | ask |
| Web hints (the current [`_WEB_HINT`](../src/tua/interactive.py#L43)) | research. Still asks before turning web on, as today. |
| A request for a document ("write a report", "summarize into a note") | knowledge |
| Anything else | Ask the user: `[a] ask  [c] code  [d] doc` |

- **Shown and overridable.** The analyzer prints one line, for example
  `→ ask · quick route · reason: question, no edit verb  [Enter] go  [c] code  [p] plan`.
- **Override evidence.** The analyzer's choice and the final choice are both stored per turn.
  The override rate is the evidence for or against an optional v2 classifier: a cheap route
  with `--json-schema`, used only if the rules are measurably wrong.
- **Coding stays explicit in uncertain cases.** When unsure, the analyzer asks rather than
  creating a worktree.

## 7. Planner and optional agent task graph

```text
/plan "<goal>"
   │
   ├─ planner: ask turn, read-only, structured output ──► plan JSON (stored, hashed)
   │
   ├─ user reviews:  [Enter] run  [e] edit in $EDITOR  [x] cancel
   │
   └─ executor, sequential v1:
        s1 researcher  ask + web    → findings.md (read-only root for later steps)
        s2 worker      code task    base = recorded HEAD
        s3 worker      code task    base = snapshot commit of s2 (peer_review._snapshot technique)
        s4 reviewer    tua review   of s3, preferring a different provider
        └─ result: s3's worktree → the normal merge (rule changed for chains; see the steps)
```

Plan schema (structured output):

```json
{"goal": "…",
 "steps": [{"id": "s1", "role": "research|code|review|ask",
            "goal": "…", "depends_on": [], "done_when": "…", "why": "…"}]}
```

- **Roles are compositions** of existing pieces:

  | Role | Built from |
  |---|---|
  | planner | ask turn with an output schema |
  | researcher | ask turn with web on, if the user allows it |
  | worker | code task |
  | reviewer | existing `tua review` |

  Steps hand work to each other through files in the plan folder and the prompt notes.
  Providers do not chat with each other.
- **Chained code steps.** Each code step starts from the snapshot commit of the previous one.
  The merge needs one rule change for chains: the user's branch must still be at the plan's
  **root** base, and the final tree is squashed onto that root. All other merge checks stay.
- **Progress view.** A terminal table, redrawn on each event and kept in SQLite so that
  `tua plan status <id>` shows it later:

  ```text
  Plan 20261025-142201-3f2a  "Add CSV export"   step 2/4
    s1 research  standard  done      12s  3 sources (2 quote_found)
    s2 code      quick     running   41s  editing src/export.py       why: implement the writer
    s3 code      quick     waiting        after s2                    why: add tests
    s4 review    second    waiting        after s3                    why: independent check
  ```

- **Limits.** The plan reuses the eval runner's budget logic: a maximum number of runs and a
  wall-clock limit, both shown before the plan starts. Ctrl+C stops the current step, keeps
  every worktree, and records the plan as `cancelled`.
- **Not in v1:**
  - parallel workers (quota and terms risk)
  - inter-agent debate
  - Claude Code's `--agents` subagents. They can be used *inside* one step later; TUA does not
    manage them.

## 8. Provider/model router and adapters

### Routes and policy (user configuration)

```yaml
# ~/.config/tua/config.yaml   (placeholders: use aliases your CLIs accept)
routes:
  quick:    {provider: claude-code, model: "<fast alias>",   effort: low,  billing: subscription}
  standard: {provider: claude-code, model: "<default alias>",              billing: subscription}
  deep:     {provider: claude-code, model: "<strong alias>", effort: high, billing: subscription}
  second:   {provider: codex,       model: "<plan model>",   effort: medium, billing: subscription}
policy:
  ask:        [quick, second]
  research:   [standard, second]
  knowledge:  [standard]
  code-small: [quick, standard]
  code-large: [deep, standard]
  review:     [second, standard]   # the router prefers a provider other than the author's
allow_api_billing: false
```

Choosing a route:

1. Take the first route in the class's list that meets all of these:
   - it has the needed capabilities (`edit` for code, `web` when web is on, `schema` for the
     planner)
   - it passed preflight in this session (cached per session)
   - it is not marked exhausted
2. On `rate_limited`, mark the route exhausted for the session, then:
   - **Ask turn** that produced no output yet: retry automatically on the next route of the
     *same billing kind*.
   - **Code task:** offer the retry (`Retry with "standard"? [Y/n]`). Never switch silently
     once files may have changed.
3. Use an `api` route only if `allow_api_billing: true`, and print "(API-billed)" each time.

Per-task flags (`--provider`, `--model`, `--effort`) keep working and bypass the policy, as today.
Each run stores its route name, the reason, and the fallback chain.

### Adapters

| Adapter | Billing | Status | Notes |
|---|---|---|---|
| `claude-code` | Subscription (Claude login), or API if the user configured a key in Claude Code itself | Exists | TUA never handles keys. The `billing` label is the user's declaration. |
| `codex` | Subscription (ChatGPT login), or API key | Exists | Model availability depends on the plan; preflight catches it. |
| `gemini-api` | **API only**, per request | Future, opt-in | Gemini CLI with a paid Gemini API key or Vertex AI. Consumer Google logins are rejected since June 18, 2026. |
| Antigravity CLI (`agy`) | Consumer plans | **Excluded** | Its FAQ says wrapping `agy` inside third-party apps is not supported on consumer plans and can get the account suspended. |

## 9. Research source registry and human verification

```text
research turn (web on) ── structured output ──► answer.md + sources[]
        │                                         {url, title, quote, supports}
        ▼
TUA check, deterministic:
  fetch url: https/http only, public addresses only, ≤ 5 MB, ≤ 20 s, no cookies or auth
  normalize the text; is the quote present?
  status: quote_found | quote_missing | unreachable | not_checked
        │
        ▼
inbox item "finding" ──► person: good | bad (reason) | promote (project|global) | dismiss
        │ promote
        ▼
knowledge note (Markdown with front matter):
  sources with their check status · origin turn · approved_at
```

- **The model's own claims never set a check status.** Only TUA's fetch does. Whether TUA may
  fetch at all is open question 6 in the audit. The alternative is a separate provider turn
  with web access whose result is labelled `model_checked`.
- **Tables** (additive):
  - `sources`: turn, URL, title, quote and its hash, content hash, HTTP status, check status,
    time
  - `inbox_items`: kind, reference, created time
  - `inbox_decisions`: append-only, one row per decision
- **Knowledge notes** are plain Markdown with front matter:

  ```markdown
  ---
  schema_version: 1
  id: k-20261025-isp-pipeline-order
  scope: project
  kind: finding            # finding | memory | reference
  title: Typical ISP block order
  sources:
    - {url: "https://…", check: quote_found, checked_at: "2026-10-25T14:22:01Z"}
  origin: {turn: "…", task: null}
  approved_at: "2026-10-25T14:30:00Z"
  ---
  Black level → lens shading → demosaic → …
  ```

  They are diffable and portable. Runs get them as read-only roots, plus an index line in the
  `[TUA]` notes. There are no embeddings; search, if needed, is a plain text search over the
  notes.
- **Notes are data, never rules.** A note cannot become a `must` rule. Turning knowledge into a
  skill goes through the proposal flow (section 10).

## 10. Feedback to skill proposal, evaluation, and approval

```text
feedback rows (good/bad, fingerprinted)        existing
      │  tua skill propose --skill project:… [--feedback ID …]
      ▼
proposal drafted        read-only run: reads feedback and skill text → unified diff + rationale
      │                 stored: patch, base skill hash, linked feedback IDs
      ▼
proposal evaluated      tua eval run SUITE --proposal ID
      │                 skills compiled from an OVERLAY (live file untouched)
      │                 compared with the pinned baseline (existing comparison and budgets)
      ▼
human decision          tua skill approve ID   → shows the eval comparison, asks y/N
      │                                         → refuses if the live file's hash ≠ proposal base
      │                                         → applies the patch, records the new version
      └──────────────── tua skill reject ID --reason …
```

- **States:** `drafted → evaluated → approved | rejected`. Every transition is recorded. Nothing
  is applied automatically.
- **Approving without an eval** requires `--without-eval`. TUA records the flag and prints that
  the change is unmeasured.
- **The overlay** reuses `compile_sources`. The proposal's bytes replace one source in memory,
  so its source and compiled hashes are real and comparable. The eval manifest records the
  proposal ID.
- **Proposals in the inbox.** A proposal appears in `tua inbox` until it is decided.

## 11. Local persistence

All additions are new tables or new nullable columns, through the existing `PRAGMA user_version`
migrations (6 and up). Nothing is dropped or rebuilt.

| Table | Purpose | Notes |
|---|---|---|
| `conversations` | One per `tua` session | Folder, project (nullable), start time |
| `turns` | Ask, knowledge, and code turns | Code turns reference `tasks.id`. Ask turns live here because `tasks.worktree_path` and `task_branch` are `NOT NULL UNIQUE`. |
| `turn_events` | Progress lines and tool actions | Append-only (trigger), bounded per turn |
| `plans`, `plan_steps` | Plans, approval, step status, reasons | A step references a task or a turn |
| `sources` | Research provenance | Per turn |
| `inbox_items`, `inbox_decisions` | Human review queue | Decisions append-only |
| `skill_versions` | Source skill bytes by hash | Files under `state/skills/` |
| `skill_proposals` | Proposal lifecycle | The patch file is stored privately; its hash is in the row |
| `route_use` (or columns on `turns`/`tasks`) | Route, reason, fallback chain | For routing evidence |

Files: `state/turns/<id>/` (results, events, staging output) and `state/plans/<id>/`
(plan JSON, step handoffs). All are `0700`/`0600`, like today. Knowledge notes and skills are the
only TUA files inside a project (`.tua/`).

## 12. Security boundaries

| Mode | Working directory | Reads | Writes | Commands | Network | Git effect |
|---|---|---|---|---|---|---|
| Ask | Live folder or scratch | The folder plus read roots; secret files and credential paths denied (Claude Code) | None | Claude Code: none, or only `dontAsk`'s built-in read-only commands (decision pending, audit Phase 0). Codex: `--sandbox read-only`. | Off; web search only if turned on for the turn | None |
| Research | Same as ask | Same | None | Same | Provider web search and fetch, opt-in per turn, recorded | None |
| Knowledge | Staging | Same | Staging only; applied by explicit copy, no silent overwrite, backup kept | Same as ask | Opt-in | None (the user commits) |
| Code | Task worktree | Worktree plus read roots (Claude Code) | Worktree only | `commands.allow` (Claude Code); OS sandbox (Codex) | Off for commands; web opt-in | `tua/<id>` branch; merge only after review |
| Plan step | Per role | Per role | Per role | Per role | Per role | As code mode, plus the chained-merge rule |

Cross-cutting rules, unchanged or extended:

- **Enforcement layers stay as documented in [security.md](security.md):**
  - TUA code: Git operations, files, approvals.
  - Claude Code rules: tool permissions, not an OS sandbox.
  - Codex's OS sandbox: limits writes and network, **not reads**. Ask turns on Codex are
    labelled as able to read any file the user can, as tasks are today.
- **Nothing lasting happens without approval:** merges, staging applies, knowledge promotions,
  skill approvals.
- **Untrusted content stays data:** web pages, repository files, model output. Notes and
  findings are labelled with their origin.
- **API billing is gated** by the user config and printed on every use.
- **Secrets:** the same refusal and redaction as today, applied to turns, sources, notes, and
  proposals.

## 13. Compatibility and migration

- **Existing commands do not change behavior:** `tua run`, `review`, `status`, `diff`, `merge`,
  `discard`, `good`, `bad`, `eval`, and `project`.
- **New commands are additive:** `ask`, `plan`, `inbox`, `skill`, `feedback list`.
- **Interactive mode changes in two steps:**
  1. Modes and follow-ups ship behind `interactive: {modes: true}` in the user config, while the
     current task-per-prompt flow stays the default.
  2. The default flips only after review. `/code` always gives today's behavior.
- **Tests stay quota-free.** The fake CLIs gain `stream-json` events, resume, and
  structured-output responses. Quote checks are tested against a local HTTP server. Each phase
  in the audit lists its acceptance tests.
