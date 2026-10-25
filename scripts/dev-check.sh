#!/usr/bin/env bash
# Run the standard local checks. Exits non-zero as soon as one fails.
# Usage: scripts/dev-check.sh            (uses `python` from the active environment)
#        PYTHON=.venv/bin/python scripts/dev-check.sh
set -euo pipefail

cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python}"

step() { printf '\n==> %s\n' "$*"; }

step "ruff format --check"
"$PYTHON" -m ruff format --check src tests

step "ruff check"
"$PYTHON" -m ruff check src tests

step "mypy (strict)"
"$PYTHON" -m mypy

step "pytest (unit + integration; live tests are skipped unless TUA_LIVE_CLAUDE=1)"
"$PYTHON" -m pytest -q

step "build sdist + wheel"
rm -rf dist
"$PYTHON" -m build --outdir dist . >/dev/null
ls -1 dist

printf '\nAll checks passed.\n'
