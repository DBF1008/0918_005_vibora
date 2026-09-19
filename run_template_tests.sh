#!/usr/bin/env bash
#
# Manual test runner for the templates sub-system refactor:
#   - tests/templates/engine_transactions.py  (transactional batch loading)
#   - tests/templates/loader_reload.py        (incremental template reloading)
#   - tests/templates/cython_sourcemap.py     (Cython source mapping)
#
# Usage:
#   ./run_template_tests.sh            # run all three modules
#   ./run_template_tests.sh -v         # verbose output
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# The templates package itself is pure Python, but importing the top-level
# `vibora` package requires its compiled Cython parser. Prefer the system
# Python 3.9 shipped with macOS Command Line Tools, which matches the
# codebase's Python 3.6-era syntax; fall back to python3 otherwise.
PYTHON_BIN="${PYTHON:-}"
if [ -z "$PYTHON_BIN" ]; then
    for candidate in /usr/bin/python3 python3.10 python3.12 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            PYTHON_BIN="$candidate"
            break
        fi
    done
fi

echo "==> Using interpreter: $PYTHON_BIN ($($PYTHON_BIN --version 2>&1))"
echo "==> Running template engine / loader / cython compiler unit tests"
echo

set -x
"$PYTHON_BIN" -m unittest \
    tests.templates.engine_transactions \
    tests.templates.loader_reload \
    tests.templates.cython_sourcemap \
    "$@"
