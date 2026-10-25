# Decision log

Constraints discovered while building v0.1 and the decisions taken. Newest entries go last.

## D1: Verify Claude Code flags by preflight, not by `--help`

**Finding (Claude Code 2.1.301).** `--append-system-prompt-file` and `--max-turns` are accepted
but **not listed** in `claude --help`. `--version` short-circuits all option parsing, so it cannot
be used for probing either.

**Decision.** Before every task, and in `tua doctor`, TUA runs the exact task argv with stdin set
to `/dev/null`. With no input, the CLI exits with `Input must be provided…` before any API call.
An unknown flag yields `unknown option '<flag>'`, and TUA turns that into
`cli_unsupported`. Any other response fails closed.

## D2: Inject skills with `--append-system-prompt-file`

This flag preserves Claude Code's default system prompt. The payload is passed by absolute path
from TUA's private state directory. TUA never writes `CLAUDE.md`, `AGENTS.md`, or
`.claude/settings.json` into a worktree, and v0.1 has no in-worktree fallback.

A live smoke test (`tests/live/`) proved that the payload reaches the model: a random marker
that existed only in the payload was written by the model.

## D3: `dontAsk` permission mode, generated settings, and `--setting-sources user`

`--permission-mode dontAsk` denies anything not pre-approved, and
`--permission-prompts none` ensures nothing waits for an answer. Allow and deny rules go into
a generated `--settings` file. The most critical deny rules are repeated in `--disallowedTools`,
because the CLI notes that in `--print` mode, settings files that fail validation are *silently
ignored*.

`--setting-sources user` keeps the user's own settings but skips repository-provided project and
local settings, because hooks there could run commands without the trust dialog that `-p` skips.
`--strict-mcp-config` disables MCP servers. The live smoke test confirmed that a command allowed
only by TUA's settings file ran and a non-allowlisted command was denied.

## D4: Prompt on stdin; parent-session variables scrubbed

The prompt goes to stdin, so it never appears in argv or `ps`. When TUA runs inside a Claude Code
session, variables such as `CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`, and
`CLAUDE_CODE_MESSAGING_TOKEN` are removed from the child environment, along with Git location
variables such as `GIT_DIR`. Everything else is inherited, because authentication, proxies, and
locale need it.

## D5: Only `loggedIn` is read from `claude auth status --json`

The output also contains email and organization details. TUA discards them, and tests check
that they never appear in `tua doctor` output.

## D6: Merge with `commit-tree` and `merge --ff-only --no-overwrite-ignore`

**Finding.** Plain `git merge --ff-only` silently overwrote an *ignored* file in the target
checkout when the task had un-ignored and added that path. `--no-overwrite-ignore` makes Git refuse
instead.

**Decision.** Squash the task snapshot into one commit on the recorded base with
`git commit-tree`, using the user's configured identity, then fast-forward with
`--no-overwrite-ignore`. A merge that fails changes nothing.

Trade-off: repository commit hooks do not run for the squash commit. This is documented in the
lifecycle guide.

## D7: Merge only onto an unchanged base

Both planning documents say to stop when the target has advanced. TUA therefore never performs a
three-way merge in the user's checkout, so it can never leave conflict markers there. Users merge
the kept `tua/<id>` branch themselves when needed.

## D8: Project config lives in the repository, guarded by a registration fingerprint

`.tua/project.yaml` matches the proposed schema (`repository: "."`, relative paths). Because a
`git pull` could change `commands.allow`, TUA records the file's SHA-256 at registration and
refuses tasks until a changed file is re-registered.

## D9: Compiled payloads are stored in the state directory

The documents allowed either the cache or the state area. The state directory (`compiled/<sha>.md`)
was chosen so the exact payload of past tasks stays inspectable. Files are content-addressed and
re-verified against their hash before each run.

## D10: Eval baselines are stored by TUA and pinned by ID and fingerprints

The architecture example's `results_file` (results inside the repository) conflicts with
"keep evaluation artifacts out of the repository by default". v0.1 stores baselines as read-only
manifests in TUA's state directory and supports no `results_file` key. A suite pins
`baseline.id` plus optional fingerprints, all of which are verified.

## D11: Wall-clock budget uses worst-case run time

A run starts only if `elapsed + timeout_seconds + 15 s` fits in `max_total_seconds`, so a
started run can never push the eval past its budget. The per-case `timeout_seconds` bounds the
whole run: provider and commands.

## D12: Interrupted tasks are detected by process identity

A task records its runner's and provider's PID, plus boot ID and start time from `/proc`, which
resists PID reuse. `tua status` marks a `preparing` or `running` task as `failed` /
`interrupted` only when the runner is gone *and* the provider is not still running.

## D13: No AI co-author trailers; identity preserved

The repository owner asked that commits carry no `Co-authored-by` trailer for AI tools. TUA's
own merge commits add only a `TUA-Task-ID:` trailer and use the repository's configured identity.

## D14: License

The repository already contained an Apache-2.0 `LICENSE`. It was preserved and declared in
`pyproject.toml`.
