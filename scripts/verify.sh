#!/usr/bin/env bash
# Release gate for migsafe: run before every push that claims "done".
# Three independent checks: install, version consistency, tests, and the CLI doing real work.
# The smoke step asserts the CLI's documented exit code, including the
# non-zero code it is supposed to return when it finds something.
set -uo pipefail
cd "$(dirname "$0")/.."

fail() { echo "GATE FAILED: $*"; exit 1; }

echo "== check 1/3: editable install"
python3 -m pip install -q -e ".[dev]" || fail "install"

echo "== check 2/4: version consistency (pyproject vs module)"
pv=$(python3 -c "import tomllib,pathlib;print(tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']['version'])")
mv=$(python3 -c "import migsafe;print(migsafe.__version__)")
[ "$pv" = "$mv" ] || { echo "pyproject says $pv, module says $mv"; fail "version drift"; }
echo "version $pv consistent"

echo "== check 3/4: test suite"
python3 -m pytest -q || fail "tests"

echo "== check 4/4: CLI smoke (expects exit 2)"
out=$(mktemp)
python3 -m migsafe.cli check examples/lock_users_table.sql > "$out" 2>&1
code=$?
if [ "$code" != "2" ]; then
  echo "expected exit 2, got $code"; cat "$out"; rm -f "$out"; fail "cli exit code"
fi
grep -qi "finding(s)" "$out" || { echo "smoke output looked wrong:"; cat "$out"; rm -f "$out"; fail "cli output"; }
rm -f "$out"
echo "smoke ok (exit 2 as documented)"
echo "ALL CHECKS PASSED"
