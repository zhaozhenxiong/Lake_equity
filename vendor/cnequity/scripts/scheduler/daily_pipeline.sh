#!/usr/bin/env bash
# B1 — Daily ingestion pipeline. Runs the schedule groups in dependency order,
# then the health check and metadata backup. Designed to be the single entry
# point a launchd/cron job fires each trading day.
#
# Groups run sequentially on purpose: the engine is pinned to workers=1 because
# mootdx is not fork-safe, and running one source-heavy group at a time avoids
# hammering the same upstream. A non-trading-day run is a cheap no-op (each
# `cne run daily` exits 0 with skipped_non_trading_day).
#
# One group failing does not abort the rest — we want as much of the day's data
# as possible — but any failure makes the pipeline exit non-zero after the
# health check reports it.
#
# A late stale-only pass is installed separately (see stale_pipeline.sh), so a
# source outage cannot hold the six-group pipeline open for half an hour. The
# old in-process delayed retry remains available as an explicit compatibility
# switch (`CNE_STALE_RETRY=1`) for callers that still want that behaviour.
#
# Usage: scripts/scheduler/daily_pipeline.sh [YYYY-MM-DD]
# Env: CNE_CONFIG, CNE_LOG_DIR, CNE_GROUPS (space-separated override),
#      CNE_GATE_GROUPS (space-separated; default "core" — failure ⇒ hard fail),
#      CNE_SOFT_FAIL_OK=1 (default) — gate OK 时东财/soft 失败只告警、exit 0；
#        设为 0 则 soft 失败仍 exit 1（国内全组日更可用），
#      CNE_STALE_RETRY=0 (default) — 兼容开关；设为 1 才在本进程收尾补抓，
#      CNE_STALE_RETRY_DELAY_SEC=1800 (default) — 兼容补抓前等多久，
#      CNE_SOURCE_HEALTH=1 (default) — 复用有效采集证据，只补探未触达来源；0 关闭，
#      CNE_SOURCE_VANTAGE=local — 当前网络出口的稳定标签，
#      CNE_TRADE_DATE (same as optional CLI arg — catch up a prior session).
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# Overridable so the control flow can be exercised against a stub instead of a
# real lake and a real network.
CNE="${CNE_BIN:-$REPO_ROOT/.venv/bin/cne}"
CONFIG="${CNE_CONFIG:-$REPO_ROOT/configs/cnequity.toml}"
LOG_DIR="${CNE_LOG_DIR:-$REPO_ROOT/data/cnequity/logs}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/daily-$(date +%Y%m%d).log"
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
  gate_out="$("$PY" "$REPO_ROOT/scripts/scheduler/scheduler_gate.py" check daily --config "$CONFIG" 2>>"$LOG_DIR/scheduler-gate.err")"
  gate_rc=$?
  if [[ "$gate_rc" -eq 3 ]]; then
    exit 0
  elif [[ "$gate_rc" -ne 0 || -z "$gate_out" ]]; then
    echo "[$(date '+%F %T')] daily gate failed (rc=$gate_rc)" >>"$LOG_DIR/scheduler-gate.err"
    exit 1
  fi
  TRADE_DATE="$gate_out"
  GATED=1
fi
# Expanded below as ${DATE_ARGS[@]+"${DATE_ARGS[@]}"}: macOS ships bash 3.2,
# where "${arr[@]}" on an empty array is an unbound-variable error under `set -u`
# (fixed in bash 4.4). Every scheduled run omits --trade-date, so the array is
# empty and the plain form killed the pipeline at its first group.
DATE_ARGS=()
if [[ -n "$TRADE_DATE" ]]; then
  DATE_ARGS=(--trade-date "$TRADE_DATE")
fi

# NB: not named GROUPS — that is a reserved bash builtin (user group IDs).
# Read the configured group order through the same Python selector used by
# `cne run daily --all-groups`; CNE_GROUPS remains an explicit host override.
GROUP_LIST="${CNE_GROUPS:-}"
if [[ -z "$GROUP_LIST" ]]; then
  if ! GROUP_LIST="$("$PY" -m cnequity.orchestrator.schedule_groups --config "$CONFIG")"; then
    echo "cannot resolve configured daily groups" >&2
    exit 1
  fi
fi
GATE_GROUP_LIST="${CNE_GATE_GROUPS:-core}"
# Overseas Mac: expected EM lag must not paint the whole day red.
SOFT_FAIL_OK="${CNE_SOFT_FAIL_OK:-1}"
# ...but a soft group that fails every day is an outage, not lag. One bad day
# is noise; three in a row is the failure mode the runbook records — default
# SOFT_FAIL_OK=1 hid a soft group for three days and nobody noticed. Track the
# consecutive-failure streak per group and escalate on persistence instead of
# on a single result. 0 disables escalation.
SOFT_FAIL_MAX_DAYS="${CNE_SOFT_FAIL_MAX_DAYS:-3}"
SOFT_STREAK_DIR="${CNE_SOFT_STREAK_DIR:-$LOG_DIR/../state/soft_streak}"
STALE_RETRY="${CNE_STALE_RETRY:-0}"
STALE_RETRY_DELAY_SEC="${CNE_STALE_RETRY_DELAY_SEC:-1800}"
SOURCE_HEALTH="${CNE_SOURCE_HEALTH:-1}"
SOURCE_VANTAGE="${CNE_SOURCE_VANTAGE:-local}"
export CNE_SOURCE_VANTAGE="$SOURCE_VANTAGE"

# `mkdir` is the portable atomic primitive available in macOS Bash 3.2. Keep
# one lock around the entire script so the independently scheduled stale pass
# cannot overlap a group, health check, or metadata backup.
. "$REPO_ROOT/scripts/scheduler/scheduler_lock.sh"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

lock_rc=0
scheduler_lock_acquire "$REPO_ROOT" daily || lock_rc=$?
if [[ "$lock_rc" -eq 1 ]]; then
  log "another daily/stale scheduler run is active — skipping"
  exit 0
elif [[ "$lock_rc" -ne 0 ]]; then
  log "unable to acquire scheduler lock (rc=$lock_rc)"
  exit 1
fi
scheduler_lock_install_traps
if [[ "$GATED" == "1" ]]; then
  # Counted as run once the lock is ours: one attempt per session.
  "$PY" "$REPO_ROOT/scripts/scheduler/scheduler_gate.py" mark daily "$TRADE_DATE" --config "$CONFIG"
fi

_is_gate_group() {
  local g="$1" x
  for x in $GATE_GROUP_LIST; do
    [[ "$x" == "$g" ]] && return 0
  done
  return 1
}

log "==== daily pipeline start $(date '+%Y-%m-%d %H:%M:%S') trade_date=${TRADE_DATE:-today} ===="
failed_groups=()
gate_failed=()
soft_failed=()
# parallel arrays: group name → OK|FAILED|…  (bash 3.2 compatible, no assoc arrays)
summary_names=()
summary_status=()
audit_run_ids=""
audit_scope_failed=0

record_audit_runs() {
  local ids
  if ids="$("$PY" -m cnequity.orchestrator.scheduler_audit "$1" 2>>"$LOG")"; then
    audit_run_ids="${audit_run_ids}${audit_run_ids:+ }${ids}"
  else
    log "cannot identify runs for the health audit"
    audit_scope_failed=1
  fi
}

for g in $GROUP_LIST; do
  log "--- group: $g ---"
  group_output="$(mktemp "$LOG_DIR/group-output.XXXXXX")" || exit 1
  if "$CNE" run daily --group "$g" --config "$CONFIG" ${DATE_ARGS[@]+"${DATE_ARGS[@]}"} >"$group_output" 2>>"$LOG"; then
    cat "$group_output" >>"$LOG"
    summary_names+=("$g")
    if grep -Eq '"status"[[:space:]]*:[[:space:]]*"skipped_non_trading_day"' "$group_output"; then
      log "group $g SKIPPED (non-trading day)"
      summary_status+=("SKIPPED")
    elif grep -Eq '"status"[[:space:]]*:[[:space:]]*"skipped_not_scheduled"' "$group_output"; then
      log "group $g SKIPPED (weekly cadence, not its day)"
      summary_status+=("SKIPPED")
    else
      log "group $g OK"
      summary_status+=("OK")
    fi
  else
    cat "$group_output" >>"$LOG"
    log "group $g FAILED (see $LOG)"
    failed_groups+=("$g")
    summary_names+=("$g")
    summary_status+=("FAILED")
    if _is_gate_group "$g"; then
      gate_failed+=("$g")
    else
      soft_failed+=("$g")
    fi
  fi
  record_audit_runs "$group_output"
  rm -f "$group_output"
done

# Second attempt at whatever is still behind, before the health check so a
# successful repair does not page anyone. `cne status --datasets --gate` exits 1 when
# something is STALE, which makes it the probe: on a clean day this costs one
# directory walk and skips the sleep entirely.
stale_retry_status="skipped"
if [[ "$STALE_RETRY" == "1" ]]; then
  log "--- stale probe ---"
  if "$CNE" status --datasets --gate --groups "$GROUP_LIST" --config "$CONFIG" >>"$LOG" 2>&1; then
    log "nothing stale — no retry needed"
    stale_retry_status="not needed"
  else
    log "something is stale; waiting ${STALE_RETRY_DELAY_SEC}s before re-fetching"
    sleep "$STALE_RETRY_DELAY_SEC"
    log "--- stale retry ---"
    retry_output="$(mktemp "$LOG_DIR/retry-output.XXXXXX")" || exit 1
    if "$CNE" run daily --stale-only --groups "$GROUP_LIST" --config "$CONFIG" \
      ${DATE_ARGS[@]+"${DATE_ARGS[@]}"} >"$retry_output" 2>>"$LOG"; then
      log "stale retry OK"
      stale_retry_status="OK"
    else
      log "stale retry FAILED (see $LOG)"
      stale_retry_status="FAILED"
      # Soft by construction: the groups already had their turn, and a source
      # still down after the wait is not something this run can fix.
      soft_failed+=("stale-retry")
    fi
    cat "$retry_output" >>"$LOG"
    record_audit_runs "$retry_output"
    rm -f "$retry_output"
  fi
fi

# Health check (fires desktop notification on problems) and backup run
# regardless of group outcomes so we always get a status signal and a snapshot.
log "--- health check ---"
# Quality errors always fail. Freshness gates only the hard-required groups
# actually scheduled here; soft groups retain the escalation policy below.
health_groups=""
for g in $GROUP_LIST; do
  if _is_gate_group "$g"; then health_groups="${health_groups:+$health_groups }$g"; fi
done
freshness_check=0
[[ -n "$health_groups" ]] && freshness_check=1
health_failed="$audit_scope_failed"
if ! CNE_GROUPS="$health_groups" CNE_FRESHNESS_CHECK="$freshness_check" CNE_AUDIT_RUN_IDS="$audit_run_ids" \
  "$REPO_ROOT/scripts/scheduler/health_notify.sh" >>"$LOG" 2>&1; then
  log "health check reported problems"
  health_failed=1
fi

# Availability evidence is non-blocking while it accumulates: a red public
# source is the observation, not a reason to discard an otherwise valid core
# revision.  `cne sources slo` still writes fail-closed incidents/report state,
# and release/acceptance gates invoke it separately with `--enforce`.
if [[ "$SOURCE_HEALTH" == "1" ]]; then
  log "--- source health (vantage=${SOURCE_VANTAGE}) ---"
  if ! "$CNE" sources probe --stale-only --config "$CONFIG" --vantage "$SOURCE_VANTAGE" >>"$LOG" 2>&1; then
    log "source probe command FAILED (non-fatal)"
  fi
  if ! "$CNE" sources slo --config "$CONFIG" >>"$LOG" 2>&1; then
    log "source SLO reporting FAILED (non-fatal)"
  fi
fi

# Persist the current consecutive-day evidence after every scheduled run.  It
# is intentionally not enforced here: the first 19 clean days must not make a
# healthy ingestion job exit non-zero.  Release governance enforces day 20.
log "--- stability evidence ---"
if ! "$CNE" verify --runs --config "$CONFIG" --days 20 >>"$LOG" 2>&1; then
  log "stability reporting FAILED (non-fatal)"
fi

log "--- backup ---"
if ! "$REPO_ROOT/scripts/scheduler/backup_meta.sh" >>"$LOG" 2>&1; then
  log "backup FAILED"
fi

# Storage maintenance is preview-only. Expired revisions and registered trials
# require review and explicit confirmation on the serve storage page.
log "--- storage preview (no deletion; confirm cleanup in serve) ---"
if ! "$CNE" run clean --config "$CONFIG" >>"$LOG" 2>&1; then
  log "staging cleanup FAILED (non-fatal)"
fi

log "---- group summary (gate=${GATE_GROUP_LIST}) ----"
i=0
while [[ $i -lt ${#summary_names[@]} ]]; do
  g="${summary_names[$i]}"
  st="${summary_status[$i]}"
  kind="soft"
  _is_gate_group "$g" && kind="gate"
  log "  ${g}: ${st}  [${kind}]"
  i=$((i + 1))
done
log "  stale-retry: ${stale_retry_status}"

# Update the per-group consecutive-failure streaks before deciding the exit
# code. A group that succeeded today has its streak cleared; one that failed
# has it incremented, and crossing the threshold escalates a warn-only day
# into a red one.
escalated=()
if [[ "$SOFT_FAIL_MAX_DAYS" != "0" ]]; then
  mkdir -p "$SOFT_STREAK_DIR" 2>/dev/null || true
  i=0
  while [[ $i -lt ${#summary_names[@]} ]]; do
    name="${summary_names[$i]}"
    streak_file="$SOFT_STREAK_DIR/$name"
    if _is_gate_group "$name" || [[ "${summary_status[$i]}" == "SKIPPED" ]]; then
      i=$((i + 1))
      continue
    fi
    if [[ "${summary_status[$i]}" == "OK" ]]; then
      rm -f "$streak_file" 2>/dev/null || true
    else
      previous=0
      previous_date=""
      failure_date="${TRADE_DATE:-$(date +%F)}"
      if [[ -f "$streak_file" ]]; then
        read -r previous_date previous <"$streak_file" || true
        # Migrate the legacy counter, which counted invocations as days.
        if [[ "$previous_date" =~ ^[0-9]+$ ]]; then
          previous="$previous_date"
          previous_date=""
        fi
      fi
      [[ "$previous" =~ ^[0-9]+$ ]] || previous=0
      current="$previous"
      if [[ "$previous_date" != "$failure_date" ]]; then current=$((previous + 1)); fi
      echo "$failure_date $current" >"$streak_file" 2>/dev/null || true
      log "  soft streak: $name failed ${current} day(s) in a row"
      if [[ $current -ge $SOFT_FAIL_MAX_DAYS ]]; then
        escalated+=("$name:${current}d")
      fi
    fi
    i=$((i + 1))
  done
fi

if [[ ${#gate_failed[@]} -gt 0 ]]; then
  log "==== daily pipeline DONE — GATE FAILED: ${gate_failed[*]} (soft also: ${soft_failed[*]:-none}) ===="
  exit 1
fi
if [[ "$health_failed" == "1" ]]; then
  log "==== daily pipeline DONE — HEALTH GATE FAILED ===="
  exit 1
fi
if [[ ${#escalated[@]} -gt 0 ]]; then
  log "==== daily pipeline DONE — SOFT GROUP DOWN ${SOFT_FAIL_MAX_DAYS}+ DAYS: ${escalated[*]} ===="
  log "     (a soft failure this persistent is an outage, not expected lag;"
  log "      raise CNE_SOFT_FAIL_MAX_DAYS or fix the source)"
  exit 1
fi
if [[ ${#soft_failed[@]} -gt 0 ]]; then
  if [[ "$SOFT_FAIL_OK" == "1" ]]; then
    log "==== daily pipeline DONE — gate OK, EM/soft FAILED (warn-only): ${soft_failed[*]} ===="
    exit 0
  fi
  log "==== daily pipeline DONE — gate OK, EM/soft FAILED: ${soft_failed[*]} ===="
  exit 1
fi
log "==== daily pipeline DONE ok ===="
