# Contributing

Thanks for helping with TUA. The project values small, readable changes with tests over
abstractions.

## Development setup

```bash
git clone https://github.com/ntuanh/TUA.git
cd TUA
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
scripts/dev-check.sh        # format check, lint, type check, tests, build
```

`scripts/dev-check.sh` exits non-zero if any step fails. CI runs the same checks on Python
3.10 to 3.13.

## Tests

- Unit tests are in `tests/unit/`. Integration tests in `tests/integration/` use real temporary
  Git repositories and a fake `claude` executable (`tests/fixtures/fake_claude.py`).
- Tests isolate `HOME`, the XDG directories, and Git configuration, and they remove any real
  `claude` from `PATH`. They need no network, account, or quota.
- `tests/live/` runs one small task against the real Claude Code CLI and **uses account quota**.
  It is skipped unless you opt in:

  ```bash
  TUA_LIVE_CLAUDE=1 TUA_LIVE_MODELS=haiku,sonnet python -m pytest tests/live -v -s
  TUA_LIVE_CODEX=1 python -m pytest tests/live/test_live_codex.py -v -s   # model: gpt-6-luna
  ```

- Add a failing test with every bug fix, and cover failure paths, not only the happy path. Never
  weaken or delete a test to make the suite pass.

## Guidelines

- Keep the v0.1 scope: no servers, daemons, routing, or automatic skill editing. Add a
  provider only when it can be tested against the real CLI.
- Run external commands with argument lists only. Never use `shell=True`, and never use
  destructive Git commands.
- Expected failures raise a `TuaError` subclass with an actionable hint.
- **Never commit secrets.** Do not add real tokens, even in tests: build fake ones at runtime, as
  `tests/unit/test_redaction.py` does.
- Do not commit local state: databases, logs, `.venv`, `.claude/settings.local.json`, or
  `.mcp.json`.
- Use descriptive commit messages (for example `fix: keep worktree when cleanup fails`).

## Pull requests

Describe the change and how you tested it, and update the documentation when behaviour changes.
Report security issues privately (see [SECURITY.md](SECURITY.md)), not in public issues.
