#!/usr/bin/env bash
# B1 — Continuous event streams (disclosures, regulatory events, news).
#
# A separate entry point from daily_pipeline.sh on purpose. These feeds publish
# on weekends and holidays, so `cne run events` is not gated on the trading
# calendar, and it takes its own `events_ingestion` lock rather than the one
# every daily group shares. This wrapper therefore also takes its own shell
# lock: an event sweep must not be skipped just because the evening batch is
# still running. The two jobs are validated to ingest disjoint datasets
# (`validate_config`), so overlapping them is safe.
#
# The groups run in the order they appear in [job.events.groups], which is how
# `regulatory_events` gets to read the announcements the group before it just
# published.
#
# Usage: scripts/scheduler/events_pipeline.sh [YYYY-MM-DD]
# Env: CNE_CONFIG, CNE_LOG_DIR, CNE_BIN, CNE_TRADE_DATE,
#      CNE_EVENTS_GROUP (one group; default: every configured group),
#      CNE_EVENTS_JITTER_SEC (default 0 — random start offset 0..N seconds),
#      CNE_EVENTS_QUIET_HOURS ("START-END" Beijing hours; default off),
#      CNE_EVENTS_QUIET_GAP_SEC (default 3600 — spacing inside quiet hours),
#      CNE_EVENTS_STATE_DIR (last-sweep markers; default $CNE_LOG_DIR/../state),
#      CNE_SCHEDULER_LOCK_DIR (or CNE_LOCK_DIR for compatibility).
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CNE="${CNE_BIN:-$REPO_ROOT/.venv/bin/cne}"
CONFIG="${CNE_CONFIG:-$REPO_ROOT/configs/cnequity.toml}"
LOG_DIR="${CNE_LOG_DIR:-$REPO_ROOT/data/cnequity/logs}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/events-$(date +%Y%m%d).log"
TRADE_DATE="${1:-${CNE_TRADE_DATE:-}}"
GROUP="${CNE_EVENTS_GROUP:-}"

# Bash 3.2 raises an unbound-variable error for an empty `${arr[@]}` under
# `set -u`; use the guarded expansion used by daily_pipeline.sh.
ARGS=()
if [[ -n "$TRADE_DATE" ]]; then
  ARGS+=(--trade-date "$TRADE_DATE")
fi
if [[ -n "$GROUP" ]]; then
  ARGS+=(--group "$GROUP")
fi

. "$REPO_ROOT/scripts/scheduler/scheduler_lock.sh"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

# Quiet hours: a feed that is nearly idle overnight does not need the daytime
# cadence. Inside the Beijing-hour window CNE_EVENTS_QUIET_HOURS ("1-7" means
# 01:00-06:59, and "23-6" wraps midnight) a tick runs only when the last sweep
# started at least CNE_EVENTS_QUIET_GAP_SEC ago (5 minutes of slack, so jitter
# and launchd drift do not push every other tick out). Nothing is lost: each
# sweep reads from the newest item the lake holds. Never applies to a run
# given an explicit date.
QUIET_HOURS="${CNE_EVENTS_QUIET_HOURS:-}"
QUIET_GAP="${CNE_EVENTS_QUIET_GAP_SEC:-3600}"
STATE_DIR="${CNE_EVENTS_STATE_DIR:-$LOG_DIR/../state}"
LAST_FILE="$STATE_DIR/events-last-${GROUP:-all}"
NOW="${CNE_EVENTS_NOW_EPOCH:-$(date +%s)}"

# Beijing has no daylight saving, so its hour is plain arithmetic on the epoch.
_in_quiet_hours() {
  local start end hour
  [[ "$QUIET_HOURS" =~ ^([0-9]{1,2})-([0-9]{1,2})$ ]] || return 1
  start=$((10#${BASH_REMATCH[1]}))
  end=$((10#${BASH_REMATCH[2]}))
  hour=$(((NOW % 86400 / 3600 + 8) % 24))
  if ((start < end)); then
    ((hour >= start && hour < end))
  elif ((start > end)); then
    ((hour >= start || hour < end))
  else
    return 1
  fi
}

if [[ -z "$TRADE_DATE" && "$QUIET_GAP" =~ ^[0-9]+$ ]] && _in_quiet_hours; then
  last=""
  [[ -r "$LAST_FILE" ]] && read -r last <"$LAST_FILE"
  if [[ "$last" =~ ^[0-9]+$ ]] && ((NOW - last < QUIET_GAP - 300)); then
    echo "[$(date '+%H:%M:%S')] quiet hours (Beijing ${QUIET_HOURS}): last sweep $((NOW - last))s ago, due every ${QUIET_GAP}s — skipping" >>"$LOG"
    exit 0
  fi
fi

# launchd fires a StartInterval agent on an exact period, so a 15-minute feed
# reaches the upstream on a metronome. A random 0..N second offset before the
# lock spreads the requests; the sweep re-reads its own window, so a late
# start loses nothing. Off by default and for runs given an explicit date.
JITTER_MAX="${CNE_EVENTS_JITTER_SEC:-0}"
if [[ -z "$TRADE_DATE" && "$JITTER_MAX" =~ ^[0-9]+$ && "$JITTER_MAX" -gt 0 ]]; then
  jitter=$((RANDOM % (JITTER_MAX + 1)))
  log "start offset ${jitter}s (CNE_EVENTS_JITTER_SEC=${JITTER_MAX})"
  sleep "$jitter"
fi

lock_rc=0
scheduler_lock_acquire "$REPO_ROOT" events || lock_rc=$?
if [[ "$lock_rc" -eq 1 ]]; then
  # A previous sweep is still running. Skipping costs nothing: every group
  # re-reads its own window on the next tick.
  log "another events sweep is active — skipping"
  exit 0
elif [[ "$lock_rc" -ne 0 ]]; then
  log "unable to acquire scheduler lock (rc=$lock_rc)"
  exit 1
fi
scheduler_lock_install_traps
# Counted from the start of an attempt, so a failing feed is not retried faster.
if [[ -z "$TRADE_DATE" && -n "$QUIET_HOURS" ]]; then
  mkdir -p "$STATE_DIR" 2>/dev/null && echo "${CNE_EVENTS_NOW_EPOCH:-$(date +%s)}" >"$LAST_FILE"
fi

log "==== events pipeline start $(date '+%Y-%m-%d %H:%M:%S') date=${TRADE_DATE:-today} group=${GROUP:-all} ===="
if "$CNE" run events --config "$CONFIG" ${ARGS[@]+"${ARGS[@]}"} >>"$LOG" 2>&1; then
  log "==== events pipeline DONE ok ===="
  exit 0
else
  status=$?
  log "events sweep FAILED (exit $status; see $LOG)"
  log "==== events pipeline DONE failed ===="
  exit "$status"
fi
