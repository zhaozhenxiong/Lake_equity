#!/usr/bin/env bash
# Small launchd/cron-compatible lock for the shell-level scheduled jobs.
#
# Python provides the shared host-aware marker transitions; macOS needs no
# shell `flock` binary. The ingestion run lock is scoped to one `cne` invocation.  The daily pipeline invokes several commands, so it needs one
# lock around the whole script; otherwise the late stale pass could start in
# the gap between two daily groups.  `mkdir` is atomic on the local filesystems
# supported by macOS and gives us a lock that works with the system Bash 3.2.
#
# Source this file from a script and call:
#
#   scheduler_lock_acquire "$REPO_ROOT" daily || ...
#   scheduler_lock_install_traps
#   ...
#   scheduler_lock_release       # optional; EXIT trap also releases it
#
# CNE_SCHEDULER_LOCK_DIR (or the older CNE_LOCK_DIR alias) can point at a
# writable directory for tests or for a repo whose data directory is elsewhere.
# Otherwise the directory is {data.root}/locks from CNE_CONFIG, the same place
# `cne serve` takes when it starts a daily or events job. The old fallback,
# $REPO_ROOT/data/cnequity/locks, missed every lake whose root was not that path.

_scheduler_lock_root() {
  local repo_root="$1"
  local config py resolved
  if [[ -n "${CNE_SCHEDULER_LOCK_DIR:-}" ]]; then
    printf '%s\n' "$CNE_SCHEDULER_LOCK_DIR"
    return 0
  fi
  if [[ -n "${CNE_LOCK_DIR:-}" ]]; then
    printf '%s\n' "$CNE_LOCK_DIR"
    return 0
  fi
  config="${CNE_CONFIG:-$repo_root/configs/cnequity.toml}"
  py="${CNE_PYTHON:-python3}"
  resolved=""
  if [[ -f "$config" ]]; then
    resolved="$("$py" -m cnequity.orchestrator.scheduler_lock --config "$config" 2>/dev/null || true)"
  fi
  case "$resolved" in
    /*|[A-Za-z]:*|[A-Za-z]:/*)
      printf '%s\n' "$resolved"
      ;;
    *)
      printf '%s\n' "$repo_root/data/cnequity/locks"
      ;;
  esac
}

scheduler_lock_acquire() {
  local repo_root="$1"
  local lock_name="${2:-daily}"
  local lock_root py token rc
  lock_root="$(_scheduler_lock_root "$repo_root")"
  # The checkout's venv when there is one; otherwise the python3 on PATH that
  # has cnequity installed (a CI runner or a pip install without a venv).
  py="${CNE_PYTHON:-}"
  if [[ -z "$py" ]]; then
    if [[ -x "$repo_root/.venv/bin/python" ]]; then
      py="$repo_root/.venv/bin/python"
    else
      py="python3"
    fi
  fi
  export SCHEDULER_LOCK_DIR="$lock_root/$lock_name.lock"
  SCHEDULER_LOCK_ROOT="$lock_root"
  SCHEDULER_LOCK_NAME="$lock_name"
  SCHEDULER_LOCK_PYTHON="$py"
  SCHEDULER_LOCK_HELD=0
  rc=0
  token="$("$py" -m cnequity.orchestrator.scheduler_lock --acquire "$lock_root" "$lock_name" --pid "$$")" || rc=$?
  if [[ "$rc" -ne 0 ]]; then
    return "$rc"
  fi
  SCHEDULER_LOCK_TOKEN="$token"
  SCHEDULER_LOCK_HELD=1
}

scheduler_lock_install_traps() {
  # Signal handlers exit and let the EXIT trap do the ownership-checked
  # release.  Clearing the signal traps avoids recursively handling a signal
  # received while the shell is unwinding.
  trap 'scheduler_lock_release' EXIT
  trap 'trap - HUP INT TERM; exit 143' HUP INT TERM
}

scheduler_lock_release() {
  if [[ "${SCHEDULER_LOCK_HELD:-0}" != "1" ]]; then
    return 0
  fi
  "$SCHEDULER_LOCK_PYTHON" -m cnequity.orchestrator.scheduler_lock \
    --release "$SCHEDULER_LOCK_ROOT" "$SCHEDULER_LOCK_NAME" --pid "$$" --token "$SCHEDULER_LOCK_TOKEN" || true
  SCHEDULER_LOCK_HELD=0
}
