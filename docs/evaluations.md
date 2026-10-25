# Evaluations

`tua eval` gives the feedback loop a minimal evidence check: does a skill change help, compared
with a pinned baseline, on a fixed set of cases? It never edits skills.

## Suite file

Suites must live under the project's `eval_dir` (default `.tua/evals`). Unknown keys are errors,
and the whole file is validated before anything runs.

```yaml
schema_version: 1
suite_id: greeting            # lowercase slug
project: myrepo               # registered project
repo_commit: main             # optional default commit for every case (resolved once)
budget:                       # required
  max_runs: 6                 # total provider runs (1-500)
  max_total_seconds: 3600     # total wall clock (1 s - 7 days)
cases:
  - id: hello
    prompt: "Add a hello() function and a test for it."
    repo_commit: 1a2b3c4      # optional; overrides the suite default
    setup: ["python -m pip install -e ."]   # run after the provider, before checks
    checks: ["python -m pytest -q"]         # each must exit 0 for the run to pass
    metric:                                 # optional
      name: failures
      command: "python scripts/count_failures.py"   # last stdout line must be a number
      direction: lower_is_better            # or higher_is_better
    runs: 3                   # repetitions (1-20)
    timeout_seconds: 900      # bounds the whole run: provider + commands
baseline:                     # pin; printed by `tua eval baseline`
  id: bl-20261025-101500-1a2b
  suite_fingerprint: "sha256:…"
  source_skill_hash: "sha256:…"
  compiled_skill_hash: "sha256:…"
```

Commands are treated as executable input:

- Every `setup`, `checks`, and `metric` command must match a `commands.allow` prefix and no deny
  prefix.
- Shell syntax is rejected. Commands are split with `shlex` and run without a shell, in the
  run's task worktree.
- All commands are displayed, and you must confirm. `--yes` skips the prompt.

## Workflow

```bash
tua eval baseline .tua/evals/greeting.yaml   # runs the suite once, stores the baseline
# paste the printed `baseline:` block into the suite file
# edit a skill, then:
tua eval run .tua/evals/greeting.yaml        # runs the candidate, compares with the pin
```

- **Baseline.** `tua eval baseline` refuses to run if the suite already has a baseline.
  `--replace` supersedes the current baseline after confirmation; the old one is kept. A baseline
  is stored only if at least one run executed and the run was not cancelled.
- **Candidate.** `tua eval run` requires a pin, then verifies it before running anything:
  - the pinned baseline exists for this suite
  - the pinned fingerprints match it
  - its manifest file is unmodified (SHA-256)
  - the suite's cases have not changed
  
  **The baseline is never re-run.**

## Baselines are immutable

A baseline is a read-only (`0400`) JSON manifest in
`~/.local/state/tua/evals/<project>/<suite>/baselines/`. Its SHA-256 is stored in the
`eval_baselines` table, where triggers reject updates to identifying columns and deletions. The
manifest records:

- the baseline ID, its eval run, and the suite fingerprint
- the runner version and the eval file path
- the default commit and the per-case commits
- source and compiled skill hashes
- CLI version, provider, model alias, budget, elapsed time, coverage
- per-case aggregates and every run: task ID, status, checks, metric, duration, reported models

The suite fingerprint covers `schema_version`, `suite_id`, `project`, `repo_commit`, and each
case's `id`, `prompt`, `repo_commit`, `setup`, `checks`, and `metric`. It deliberately excludes
`budget`, `runs`, `timeout_seconds`, and the `baseline` pin.

## Budgets

- **Run count.** No run starts once `max_runs` provider runs have started.
- **Wall clock.** A run starts only if
  `elapsed + timeout_seconds + 15 s overhead <= max_total_seconds`. A run therefore never pushes
  the eval past its budget, even in the worst case.
- **Overrides.** `--max-runs` and `--max-seconds` override the suite's values for one invocation.
- **Coverage.** Skipped runs are recorded as `skipped_budget`. A partial result is reported as
  `INCOMPLETE` and exits with code 1.
- **Cancellation.** Ctrl+C cancels the current run, keeps its worktree, marks the remaining runs
  `skipped_cancelled`, and exits with code 130.

## Run outcomes

| Outcome | Meaning |
|---|---|
| `passed` | All checks passed. |
| `checks_failed` | At least one check failed. |
| `setup_failed` | A setup command failed. |
| `provider_failed` | The provider task did not complete. |
| `timeout` | The run hit its deadline. |
| `cancelled` | The run was cancelled. |
| `skipped_budget` | The run would not fit in the budget. |
| `skipped_cancelled` | The eval was cancelled before this run. |

Command logs and the diff (stat, plus a patch capped at 1 MiB) are saved, redacted, under the
eval run's artifact directory. Worktrees of completed runs are discarded after measurement unless
`--keep-worktrees` is given. Worktrees of runs that did not complete are always kept.

## Reading comparisons

Per case, TUA prints the baseline-to-candidate pass counts and metric means, direction-aware.
It flags:

- cases whose commit differs between baseline and candidate, because the results are then
  confounded by code changes
- incomplete coverage
- identical skill hashes

A few LLM runs are noisy. TUA never claims statistical significance. Treat the comparison as
evidence to inspect, not as proof.
