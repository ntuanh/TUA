# Task lifecycle and recovery

## States

| State | Meaning |
|---|---|
| `created` | The task row exists. It moves to `preparing` in the same transaction. |
| `preparing` | Skills are compiled, permissions generated, the provider checked, the worktree created. |
| `running` | The provider process has started. |
| `completed` | The provider exited 0 and reported success. This does not mean the change is correct. |
| `failed` | Setup or the provider failed. `error_type` says why. |
| `timeout` | The wall-clock limit was reached and the provider process group was terminated. |
| `cancelled` | Ctrl+C, SIGTERM, or SIGHUP; the provider process group was terminated. |
| `merged` | Squash-merged into the target branch by `tua merge`. |
| `discarded` | Worktree and branch removed by `tua discard` (or by an eval run's cleanup). |

```text
created -> preparing -> running -> completed | failed | timeout | cancelled
preparing -> failed | cancelled
completed -> merged | discarded
failed | timeout | cancelled -> discarded | merged (--force + second confirmation)
```

Every transition is validated against this table. It is written together with its event in one
SQLite transaction, as a compare-and-set on the previous state. Feedback (`tua good` / `tua bad`)
never changes the state.

### Error types

| Error type | Cause |
|---|---|
| `cli_missing` | `claude` is not on `PATH`. |
| `cli_unsupported` | The CLI rejected a flag, or gave an unexpected preflight response. |
| `unauthenticated` | Not logged in, or a 401 or distinctive auth message from the provider. |
| `rate_limited` | A 429, or a distinctive usage-limit message. |
| `provider_error` | The provider reported an error that TUA does not classify further. |
| `invalid_output` | Exit 0 but no valid JSON result. |
| `timeout` | The wall-clock limit was reached. |
| `cancelled` | The task was cancelled by the user or a signal. |
| `config_error`, `skill_error`, `worktree_error`, `internal_error` | TUA-side failures. |
| `interrupted` | The TUA process died mid-task. See recovery below. |

## Worktree rules

- **Recording before execution.** The source path, target branch, base commit, task branch
  (`tua/<id>`), and worktree path (`~/.local/state/tua/worktrees/<id>`) are recorded before the
  provider starts.
- **Clean checkout required.** The source checkout must be clean, including untracked files
  (ignored files are fine). TUA explains the refusal and never stashes, resets, or deletes
  anything.
- **Provider working directory.** The provider runs with the task worktree as its working
  directory, never your checkout.
- **Kept for inspection.** Worktrees of `failed`, `timeout`, `cancelled`, and interrupted tasks
  are kept. `tua diff` works on them.
- **Removal.** A worktree and its branch are removed only after a successful merge or a
  confirmed discard. Removal validates that the path is inside TUA's worktree directory and is
  a registered worktree of the same repository.

## Merge strategy

TUA uses one strategy, a squash commit with a fast-forward:

1. **Refusal checks.** The merge is refused when:
   - the task is not `completed` and `--force` was not given
   - the target checkout has a merge or rebase in progress
   - the target checkout is on a different branch
   - the target checkout is dirty
   - the target has moved from the base commit
   - the task worktree has unresolved conflicts
   - the task worktree has embedded Git repositories
   - the task made no changes
2. **Snapshot.** The worktree is snapshotted into a tree object through a temporary index. This
   includes new untracked files and any commits the provider made, but not ignored files.
3. **Review.** The full diff is shown and you confirm. For `--force` you confirm again by typing
   the task ID. `--yes` cannot answer either force confirmation.
4. **Re-check.** The checks run again, and the merge is refused if the worktree changed while you
   were reviewing it.
5. **Commit.** `git commit-tree <tree> -p <base>` creates one commit with your configured
   identity. The message ends with a `TUA-Task-ID:` trailer and carries no co-author trailers.
   Repository commit hooks do not run for this commit.
6. **Fast-forward.** `git merge --ff-only --no-overwrite-ignore <commit>` updates your branch.
   `--no-overwrite-ignore` matters: without it, Git silently overwrites ignored files in your
   checkout.
7. **Cleanup.** The worktree and branch are removed. If that fails, the merge still stands,
   `cleanup_status` becomes `failed`, and TUA prints the manual commands.

Nothing is pushed. Interrupts are deferred during the short fast-forward and database update.

## Reviews (`tua review`)

`tua review <TASK_ID> [--model M] [--focus TEXT]` runs a second, ordinary task:

1. TUA snapshots the reviewed task's result into a commit on top of its base. For a merged
   task it uses the merged commit.
2. The review task starts from that snapshot with the `read-only` profile, so the reviewer can
   read files but cannot edit or run commands. Its prompt contains the original task prompt
   and the diff (truncated beyond 60,000 characters).
3. The review is recorded like any task: own ID, events, fingerprints, model metadata. It is
   linked to the reviewed task through `review_of`, and `tua status <TASK_ID>` lists it.
   The full review text is saved as `result.md` in the review's task directory.
4. A completed review's worktree is discarded automatically when it contains no changes
   (`--keep-worktree` keeps it). A failed review keeps it, like any failed task. If the
   reviewer changed files despite the read-only profile, or the copy cannot be removed, TUA
   keeps it and says so, with its path: inspect it with `tua diff <REVIEW_ID>` and remove it
   with `tua discard <REVIEW_ID>`. Interactive mode reports the same.

Reviewing a review, an active task, a discarded task, or a task without changes is refused.
A model review is an extra opinion, not a verdict.

## Recovery guide

| Situation | What to do |
|---|---|
| Failed, timed-out, or cancelled task | `tua status <id>` (error, logs, session) and `tua diff <id>`. Then `tua discard <id>`, or `tua merge <id> --force` if you really want the partial result. |
| Inspect the provider session | `cd <worktree> && claude --resume <session_id>` (the session ID is shown in `tua status <id>`). |
| TUA was killed (power loss, SIGKILL) | `tua status` reconciles the task to `failed` / `interrupted`, unless the provider process is still running. In that case it warns and shows `kill -TERM -<pid>`. |
| Target branch moved before merging | The task branch `tua/<id>` is kept. Merge or cherry-pick it yourself with Git, then `tua discard <id>`, or discard and re-run. |
| Ignored or untracked file would be overwritten | Move the file away and run `tua merge <id>` again. Nothing was changed. |
| Cleanup failed after a merge (for example a locked worktree) | Fix the cause (`git worktree unlock <path>`), then `tua discard <id>` to retry the cleanup. |
| Worktree deleted by hand | `tua discard <id>` removes Git's stale metadata and the branch. |
| Corrupt state database | Move it aside (`mv ~/.local/state/tua/tua.db{,.broken}`). TUA starts fresh and never deletes it. |

## Process handling

The provider runs in its own session and process group, with the prompt on stdin. On timeout or
cancellation, TUA sends SIGTERM to the group, waits up to 10 seconds (a second Ctrl+C skips the
wait), then sends SIGKILL. After a normal exit, leftover background processes in the group are
stopped too. A process that escapes the group (for example with `setsid`) cannot be tracked;
this is a documented limitation.

## Exit codes of `tua run`

| Code | Meaning |
|---|---|
| `0` | `completed` |
| `1` | `failed`, or an error before the task was created |
| `124` | `timeout` |
| `130` | `cancelled` |
