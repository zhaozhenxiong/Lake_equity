#!/usr/bin/env bash
# B1 — Late stale-only repair pass.
#
# This is intentionally a separate launchd/cron job.  The normal six-group
# daily pipeline must finish and report promptly even when a source is down;
# waiting 30 minutes inside that process made a single outage look like a
# multi-hour scheduler overrun.  A late second window still repairs
# snapshot-only datasets without blocking the normal run.
#
# Usage: scripts/scheduler/stale_pipeline.sh [YYYY-MM-DD]
# Env: CNE_CONFIG, CNE_LOG_DIR, CNE_BIN, CNE_TRADE_DATE,
#      CNE_SCHEDULER_LOCK_DIR (or CNE_LOCK_DIR for compatibility).
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CNE="${CNE_BIN:-$REPO_ROOT/.venv/bin/cne}"
CONFIG="${CNE_CONFIG:-$REPO_ROOT/configs/cnequity.toml}"
LOG_DIR="${CNE_LOG_DIR:-$REPO_ROOT/data/cnequity/logs}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/stale-$(date +%Y%m%d).log"
TRADE_DATE="${1:-${CNE_TRADE_DATE:-}}"
# The checkout's venv when there is one; otherwise the python3 on PATH.
if [[ -n "${CNE_PYTHON:-}" ]]; then
  PY="$CNE_PYTHON"
elif [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  PY="$REPO_ROOT/.venv/bin/python"
else
  PY="python3"
fi
# Launched by the scheduler (hourly, CNE_SCHEDULED=1): run only when this
# session's Beijing run_at has passed and it has not run yet. A manual run —
# or one given an explicit trade date — is never gated.
GATED=0
if [[ "${CNE_SCHEDULED:-0}" == "1" && -z "$TRADE_DATE" ]]; then
  gate_out="$("$PY" "$REPO_ROOT/scripts/scheduler/scheduler_gate.py" check stale --config "$CONFIG" 2>>"$LOG_DIR/scheduler-gate.err")"
  gate_rc=$?
  if [[ "$gate_rc" -eq 3 ]]; then
    exit 0
  elif [[ "$gate_rc" -ne 0 || -z "$gate_out" ]]; then
    echo "[$(date '+%F %T')] stale gate failed (rc=$gate_rc)" >>"$LOG_DIR/scheduler-gate.err"
    exit 1
  fi
  TRADE_DATE="$gate_out"
  GATED=1
fi
# Respect the deployment's source policy, just like the main daily run.
# A banned egress can pause push2 in its config or CNE_PUSH2_PAUSED; do not
# disable snapshot repairs for every installation. Shared budgets, cached
# snapshots and refusal breakers still apply to this second window.

# Bash 3.2 raises an unbound-variable error for an empty `${arr[@]}` under
# `set -u`; use the guarded expansion used by daily_pipeline.sh.
DATE_ARGS=()
GROUP_ARGS=()
if [[ -n "${CNE_GROUPS:-}" ]]; then
  GROUP_ARGS=(--groups "$CNE_GROUPS")
fi
if [[ -n "$TRADE_DATE" ]]; then
  DATE_ARGS=(--trade-date "$TRADE_DATE")
fi

. "$REPO_ROOT/scripts/scheduler/scheduler_lock.sh"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

lock_rc=0
scheduler_lock_acquire "$REPO_ROOT" daily || lock_rc=$?
if [[ "$lock_rc" -eq 1 ]]; then
  # launchd can fire this while the main pipeline is still finishing.  It is
  # safe to skip because the next scheduled window (or a manual invocation)
  # can try again, and a duplicate run must never queue behind ingestion.
  log "another daily/stale scheduler run is active — skipping"
  exit 0
elif [[ "$lock_rc" -ne 0 ]]; then
  log "unable to acquire scheduler lock (rc=$lock_rc)"
  exit 1
fi
scheduler_lock_install_traps
if [[ "$GATED" == "1" ]]; then
  # Counted as run once the lock is ours: one attempt per session.
  "$PY" "$REPO_ROOT/scripts/scheduler/scheduler_gate.py" mark stale "$TRADE_DATE" --config "$CONFIG"
fi

log "==== stale pipeline start $(date '+%Y-%m-%d %H:%M:%S') trade_date=${TRADE_DATE:-latest} ===="
log "--- stale-only repair ---"
# Snapshot datasets only: they have no history to catch up tomorrow, so a
# failed one is retried tonight. History datasets wait for the next daily run,
# which fetches every date since their watermark anyway.
if "$CNE" run daily --stale-only --snapshots-only --config "$CONFIG" \
  ${GROUP_ARGS[@]+"${GROUP_ARGS[@]}"} \
  ${DATE_ARGS[@]+"${DATE_ARGS[@]}"} >>"$LOG" 2>&1; then
  log "stale-only repair OK"
  log "==== stale pipeline DONE ok ===="
  exit 0
else
  status=$?
  log "stale-only repair FAILED (exit $status; see $LOG)"
  log "==== stale pipeline DONE failed ===="
  exit "$status"
fi
