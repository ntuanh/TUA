# TUA user guide: using TUA in any folder

TUA is installed once for your user (with `pipx`), so the `tua` command works in every
terminal and every folder. There is nothing to activate.

```bash
tua --version    # should print: tua 0.1.0
tua doctor       # checks Git, Claude Code, and your Claude login
```

If you see `tua: command not found`, run `pipx ensurepath`, then open a new terminal.

---

## 0. The easy way: just type `tua`

Open a terminal **in your project folder** and type `tua`:

```bash
cd ~/code/myapp
tua
```

**The first time**, TUA asks a few yes/no questions. Press Enter to accept the suggested
answer:

1. `Set up TUA for this project now? [Y/n]`: press Enter.
2. `Let the AI run "python -m pytest" to test its own work? [Y/n]`: press Enter. This appears
   only if TUA recognizes your test tool.
3. `Save TUA's settings in Git? [y/N]`: press Enter to keep them only on this laptop.

**Every time after that**, `tua` goes straight to:

```text
What should the AI do? (Enter on an empty line to quit)
>
```

Type your request and press Enter. When the AI is done, you see what it changed and choose:

| Key | What it does |
|---|---|
| `m` | merge: keep the change (one local commit; nothing is pushed) |
| `d` | show the full diff |
| `r` | ask another model to review the change |
| `x` | discard the change |
| Enter | keep it for later (`tua merge <ID>` or `tua discard <ID>`) |

Tips for typing a request:
- Be specific: what to change, where, and how to check it ("add a test", "run the tests").
- End a line with `\` to continue on the next line.
- For a long request, write it in a file and type `@path/to/file`.
- To quit, press Enter on an empty line (or Ctrl+D).

The sections below describe the same steps as separate commands, which give you more options
(another model, Codex, timeouts).

---

## 1. Use TUA in a new folder

TUA works on **Git repositories**. Pick the folder you want to work on, for example `~/code/myapp`.

### Step 1: Make sure the folder is a Git repository with at least one commit

Skip this step if the folder is already a Git repository with commits.

```bash
cd ~/code/myapp
git init
git add .
git commit -m "Initial commit"
```

### Step 2: Register the folder with TUA (once per folder)

```bash
tua project init ~/code/myapp --name myapp
```

This creates two files inside the folder:

- `.tua/project.yaml` holds the settings for this project.
- `.tua/skills/project-defaults.md` holds the rules the AI follows in this project.

The name must use lowercase letters, numbers, `.`, `_` or `-`.

### Step 3: Commit the new files

TUA only starts a task when the folder has no uncommitted changes.

```bash
cd ~/code/myapp
git add .tua
git commit -m "Add TUA config"
```

If you do not want the `.tua/` folder in your Git history, keep it local instead:

```bash
echo ".tua/" >> .git/info/exclude
```

### Step 4: Run a task

You can run this from any folder; `--project` tells TUA which repository to use.

```bash
tua run "Add a README section that explains how to install the app" --project myapp
```

To choose a model, add `--model haiku` (fastest, uses the least quota), `--model sonnet`, or
`--model opus`. Haiku is a good default; use Sonnet for reviews or hard fixes.

To let the AI only look at the code (for example to explain it), add `--profile read-only`.

The "cost" TUA shows is Claude Code's API-equivalent estimate. On a Pro subscription you
are not billed per token; the task only uses your plan's quota.

The AI works in a separate copy of your project, so your folder is not changed yet.

### Step 5: Review, then keep it or throw it away

```bash
tua status                   # list tasks and their IDs
tua diff <TASK_ID>           # see exactly what changed
tua merge <TASK_ID>          # keep it: shows the diff, asks, makes one local commit
tua discard <TASK_ID>        # throw it away (asks first)
```

`tua merge` never pushes to GitHub. Push it yourself when you are happy:

```bash
git -C ~/code/myapp push
```

### Step 6 (optional): Ask a second model to review the change

```bash
tua review <TASK_ID> --model sonnet
tua review <TASK_ID> --model opus --focus "input validation"
```

The review runs as its own read-only TUA task: the reviewer can read the code but cannot
edit files or run commands, and the review is saved (`tua status <TASK_ID>` lists it).
Treat it as an extra opinion: a review can raise false alarms or miss bugs, so confirm
findings with tests.

### Step 7 (optional): Rate the result

```bash
tua good <TASK_ID> --note "did what I asked"
tua bad <TASK_ID> --reason "changed files I did not ask for"
```

---

## 2. Let the AI run commands (tests, linters)

By default the AI can read and edit files but **cannot run shell commands**, so it cannot
run your tests. The easiest fix is to allow them when you register the folder:

```bash
tua project init ~/code/myapp --name myapp --allow "python -m pytest"
```

For a folder that is already registered, edit `.tua/project.yaml` in your project:

```yaml
commands:
  allow:
    - python -m pytest
    - npm test
  deny: []
```

Then approve the change. TUA asks you to do this every time the file changes:

```bash
tua project init ~/code/myapp --name myapp
git -C ~/code/myapp commit -am "Allow tests in TUA"
```

Only allow commands you trust. An allowed test command can run any code in your project.

---

## 3. Use ChatGPT (Codex) instead of Claude

If you are logged in to Codex (`codex login`), any task or review can use it:

```bash
tua run "Fix the failing date parser" --project myapp --provider codex --model gpt-6-luna --effort medium
tua review <TASK_ID> --provider codex --model gpt-6-luna
```

Pick a model your ChatGPT plan includes; on ChatGPT Go, `gpt-6-luna` works. To make Codex the
default for a project, set this in `.tua/project.yaml` and run `tua project init` again:

```yaml
default_provider: codex
default_model: gpt-6-luna
effort: medium
```

Codex keeps tasks safe with its own sandbox. The AI can run any command, but it can only write
inside the task folder (and `/tmp`) and has no network. Your `commands.allow` list is not used
for Codex.

A good habit is to let one model write the change and another review it:

```bash
tua run "Add input validation" --project myapp --model haiku
tua review <TASK_ID> --provider codex --model gpt-6-luna
```

---

## 4. Several folders

Register each folder once with its own name, then choose one with `--project`:

```bash
tua project init ~/code/website --name website
tua project init ~/code/api --name api
tua project list
tua run "Fix the broken link in the footer" --project website
```

---

## 5. Common messages and what to do

| Message | What to do |
|---|---|
| `has uncommitted or untracked changes` | Commit your work (or `git stash -u` yourself), then run again. |
| `has no commits yet` | Make a first commit (see Step 1). |
| `is not inside a Git repository` | Run `git init` in the folder first. |
| `changed since project ... was registered` | Run `tua project init <PATH> --name <NAME>` again. |
| Task status `rate_limited` | You reached your Claude plan's limit. Wait for it to reset; do not buy credits unless you want to. |
| Task status `failed` or `timeout` | Run `tua status <TASK_ID>` for the reason, and `tua diff <TASK_ID>` to see partial work. |
| `has moved from the task's base` when merging | You committed something after starting the task. Discard it and run the task again. |

---

## 6. Update or remove TUA

```bash
cd ~/solo/TUA && git pull     # update; takes effect immediately (editable install)
pipx uninstall tua            # remove the tua command
```

TUA keeps its own data in `~/.local/state/tua/`. Your projects only contain the `.tua/` folder.

---

## 7. For developing TUA itself

When you change TUA's own code in `~/solo/TUA`, use its virtual environment to run the checks:

```bash
cd ~/solo/TUA
source .venv/bin/activate     # "activate" the development environment
scripts/dev-check.sh          # format, lint, type check, tests, build
deactivate                    # leave the environment
```
