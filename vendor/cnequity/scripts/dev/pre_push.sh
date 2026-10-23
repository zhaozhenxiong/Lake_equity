#!/usr/bin/env bash
# Pre-push gate: the unit suite on the repo venv and on Python 3.10, in parallel.
#
# CI runs 3.10 through 3.14 on three platforms, but a clone usually has one
# interpreter, so 3.11+-only APIs (datetime.UTC, tomllib) went red only after
# the push. The 3.10 run uses uv and a cached `.venv-py310`; without uv it is
# skipped with a warning. Bypass once with `git push --no-verify`.
set -uo pipefail

cd "$(git rev-parse --show-toplevel)" || exit 1
logs="$(mktemp -d)"
trap 'rm -rf "$logs"' EXIT

py="${CNE_PYTHON:-.venv/bin/python}"
if [ ! -x "$py" ]; then
  py="$(command -v python3)"
fi

"$py" -m pytest tests/unit -q -x -p no:cacheprovider >"$logs/main.txt" 2>&1 &
main_pid=$!

py310_pid=""
if command -v uv >/dev/null 2>&1; then
  UV_PROJECT_ENVIRONMENT=.venv-py310 uv run --frozen --quiet --python 3.10 --group dev \
    python -m pytest tests/unit -q -x -p no:cacheprovider >"$logs/py310.txt" 2>&1 &
  py310_pid=$!
else
  echo "pre-push: uv not found, skipping the Python 3.10 run" >&2
fi

status=0
if ! wait "$main_pid"; then
  echo "pre-push: unit tests failed on $("$py" -V 2>&1)" >&2
  tail -n 40 "$logs/main.txt" >&2
  status=1
fi
if [ -n "$py310_pid" ] && ! wait "$py310_pid"; then
  echo "pre-push: unit tests failed on Python 3.10" >&2
  tail -n 40 "$logs/py310.txt" >&2
  status=1
fi
exit "$status"
