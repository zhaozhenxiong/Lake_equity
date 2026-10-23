"""Regression tests for the macOS/cron scheduler wrappers.

These tests intentionally drive the shell entry points with a tiny fake ``cne``
binary.  They do not load launchd agents or touch the user's LaunchAgents
directory.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cnequity.orchestrator.scheduler_lock import host_identity

ROOT = Path(__file__).resolve().parents[2]
DAILY = ROOT / "scripts" / "scheduler" / "daily_pipeline.sh"
STALE = ROOT / "scripts" / "scheduler" / "stale_pipeline.sh"
EVENTS = ROOT / "scripts" / "scheduler" / "events_pipeline.sh"
DAILY_PLIST = ROOT / "scripts" / "scheduler" / "launchd" / "com.cnequity.daily.plist.template"
STALE_PLIST = ROOT / "scripts" / "scheduler" / "launchd" / "com.cnequity.stale.plist.template"
EVENTS_PLIST = ROOT / "scripts" / "scheduler" / "launchd" / "com.cnequity.events.plist.template"
NEWS_PLIST = ROOT / "scripts" / "scheduler" / "launchd" / "com.cnequity.events-news.plist.template"


def _ensure_unix_shell() -> None:
    if sys.platform == "win32":
        pytest.skip("scheduler wrappers are Unix shell scripts")


def _run(
    script: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    _ensure_unix_shell()
    return subprocess.run(
        [str(script), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _popen(script: Path, env: dict[str, str]) -> subprocess.Popen[str]:
    _ensure_unix_shell()
    return subprocess.Popen(
        [str(script)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )


def _stub_cne(tmp_path: Path) -> Path:
    path = tmp_path / "cne"
    path.write_text(
        """#!/bin/sh
printf 'argc=%s\\n' "$#" >> "$CNE_CALL_LOG"
for arg in "$@"; do printf '<%s>\\n' "$arg" >> "$CNE_CALL_LOG"; done
sleep "${CNE_STUB_SLEEP:-0}"
case "$1" in
  run) printf '{"run_id":"stub-%s"}\\n' "$$"; exit "${CNE_STUB_STATUS:-0}" ;;
  *) exit "${CNE_STUB_CHECK_STATUS:-0}" ;;
esac
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _stale_env(tmp_path: Path, cne: Path, calls: Path) -> dict[str, str]:
    # Never inherit scheduler paths or switches from the operator's lake.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CNE_") or key == "CNE_TEST_NO_NETWORK"
    }
    env.update(
        {
            "CNE_BIN": str(cne),
            "CNE_CALL_LOG": str(calls),
            "CNE_CONFIG": str(tmp_path / "cnequity.toml"),
            "CNE_LOG_DIR": str(tmp_path / "logs"),
            "CNE_SCHEDULER_LOCK_DIR": str(tmp_path / "locks"),
            "CNE_DATA_ROOT": str(tmp_path / "lake"),
            "CNE_BACKUP_DIR": str(tmp_path / "backups"),
            "CNE_NOTIFY": "0",
            "CNE_SOURCE_HEALTH": "0",
        }
    )
    return env


def _call_args(calls: Path) -> list[str]:
    lines = calls.read_text(encoding="utf-8").splitlines()
    return [line[1:-1] for line in lines if line.startswith("<") and line.endswith(">")]


def test_daily_template_schedules_all_groups_and_disables_inline_wait():
    payload = plistlib.loads(DAILY_PLIST.read_bytes())
    assert payload["Label"] == "com.cnequity.daily"
    assert payload["EnvironmentVariables"]["CNE_GROUPS"] == "__GROUPS__"
    assert payload["EnvironmentVariables"]["CNE_STALE_RETRY"] == "0"

    source = DAILY.read_text(encoding="utf-8")
    assert 'STALE_RETRY="${CNE_STALE_RETRY:-0}"' in source
    # Keep the old sleep path available only behind the explicit compatibility
    # switch; the installed plist above turns it off.
    assert 'sleep "$STALE_RETRY_DELAY_SEC"' in source


def test_stale_template_is_a_late_independent_agent():
    payload = plistlib.loads(STALE_PLIST.read_bytes())
    assert payload["Label"] == "com.cnequity.stale"
    assert payload["ProgramArguments"][-1].endswith("scripts/scheduler/stale_pipeline.sh")
    # Hourly wake-up; scheduler_gate.py picks the run by Beijing time.
    assert payload["StartCalendarInterval"] == {"Minute": 37}
    assert payload["EnvironmentVariables"]["CNE_SCHEDULED"] == "1"


def test_events_template_runs_every_calendar_day_on_its_own_lock():
    """The whole point: no trading-day gate, and not behind the daily lock."""
    payload = plistlib.loads(EVENTS_PLIST.read_bytes())
    assert payload["Label"] == "com.cnequity.events"
    assert payload["ProgramArguments"][-1].endswith("scripts/scheduler/events_pipeline.sh")
    # A weekday filter here would put the market calendar back in the path.
    assert "Weekday" not in payload["StartCalendarInterval"]
    assert 'scheduler_lock_acquire "$REPO_ROOT" events' in EVENTS.read_text(encoding="utf-8")


def test_events_pipeline_forwards_group_and_date_and_returns_cne_failure(tmp_path):
    cne = _stub_cne(tmp_path)
    calls = tmp_path / "calls"
    env = _stale_env(tmp_path, cne, calls)
    env["CNE_STUB_STATUS"] = "5"
    env["CNE_EVENTS_GROUP"] = "news_wire"

    result = _run(EVENTS, "2026-08-30", env=env)

    assert result.returncode == 5
    assert _call_args(calls) == [
        "run",
        "events",
        "--config",
        str(tmp_path / "cnequity.toml"),
        "--trade-date",
        "2026-08-30",
        "--group",
        "news_wire",
    ]
    assert "FAILED" in result.stdout


def _recording_sleep(tmp_path: Path, env: dict[str, str]) -> Path:
    """Put a ``sleep`` that only records its argument first on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "sleeps"
    stub = bin_dir / "sleep"
    stub.write_text('#!/bin/sh\necho "$1" >> "$CNE_SLEEP_LOG"\n', encoding="utf-8")
    stub.chmod(0o755)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["CNE_SLEEP_LOG"] = str(log)
    return log


def test_scheduled_events_start_at_a_random_offset_before_the_lock(tmp_path):
    cne = _stub_cne(tmp_path)
    calls = tmp_path / "calls"
    env = _stale_env(tmp_path, cne, calls)
    sleeps = _recording_sleep(tmp_path, env)
    env["CNE_EVENTS_JITTER_SEC"] = "120"

    offsets = []
    for _ in range(12):
        result = _run(EVENTS, env=env)
        assert result.returncode == 0
        offsets.append(int(result.stdout.split("start offset ")[1].split("s")[0]))

    assert all(0 <= offset <= 120 for offset in offsets)
    assert len(set(offsets)) > 1, "a fixed offset is still a metronome"
    # The offset is slept before the sweep runs, and it is the first sleep.
    assert sleeps.read_text(encoding="utf-8").splitlines()[0] == str(offsets[0])
    assert _call_args(calls)[:2] == ["run", "events"]


def test_events_offset_is_off_by_default_and_for_explicit_dates(tmp_path):
    cne = _stub_cne(tmp_path)
    calls = tmp_path / "calls"
    env = _stale_env(tmp_path, cne, calls)
    sleeps = _recording_sleep(tmp_path, env)

    unset = _run(EVENTS, env=env)
    env["CNE_EVENTS_JITTER_SEC"] = "120"
    dated = _run(EVENTS, "2026-08-30", env=env)

    assert unset.returncode == dated.returncode == 0
    assert "start offset" not in unset.stdout + dated.stdout
    assert set(sleeps.read_text(encoding="utf-8").split()) == {"0"}  # the stub cne's own sleep


def _quiet_env(tmp_path: Path, *, beijing: str, last_ago: int | None) -> tuple[dict, Path]:
    """Env that pretends it is Beijing ``beijing`` (HH:MM) on 2026-10-18."""
    cne = _stub_cne(tmp_path)
    calls = tmp_path / "calls"
    env = _stale_env(tmp_path, cne, calls)
    hour, minute = map(int, beijing.split(":"))
    now = datetime(2026, 10, 18, hour, minute, tzinfo=timezone(timedelta(hours=8)))
    env.update(
        {
            "CNE_EVENTS_NOW_EPOCH": str(int(now.timestamp())),
            "CNE_EVENTS_STATE_DIR": str(tmp_path / "state"),
            "CNE_EVENTS_QUIET_HOURS": "1-7",
            "CNE_EVENTS_QUIET_GAP_SEC": "3600",
            "CNE_EVENTS_GROUP": "news_wire",
        }
    )
    if last_ago is not None:
        marker = tmp_path / "state" / "events-last-news_wire"
        marker.parent.mkdir()
        marker.write_text(f"{int(now.timestamp()) - last_ago}\n", encoding="utf-8")
    return env, calls


@pytest.mark.parametrize(
    ("beijing", "last_ago", "runs"),
    [
        ("03:07", 900, False),  # quiet hours, swept 15 min ago
        ("03:07", 3300, True),  # an hour tick, 5 minutes of slack
        ("03:07", 7200, True),
        ("03:07", None, True),  # never swept: run
        ("00:52", 900, True),  # before the window
        ("07:00", 900, True),  # the window is [01:00, 07:00)
        ("06:59", 900, False),
        ("01:00", 900, False),
    ],
)
def test_events_quiet_hours_thin_the_ticks_without_dropping_the_hour(
    tmp_path, beijing, last_ago, runs
):
    env, calls = _quiet_env(tmp_path, beijing=beijing, last_ago=last_ago)

    result = _run(EVENTS, env=env)

    assert result.returncode == 0
    assert calls.exists() is runs
    if not runs:
        log = next((tmp_path / "logs").glob("events-*.log")).read_text(encoding="utf-8")
        assert "quiet hours" in log and "skipping" in log


def test_events_quiet_hours_count_from_the_start_of_each_attempt(tmp_path):
    env, calls = _quiet_env(tmp_path, beijing="03:07", last_ago=7200)

    assert _run(EVENTS, env=env).returncode == 0
    assert calls.exists()
    calls.unlink()
    # 15 minutes later: the attempt above reset the clock.
    later = datetime(2026, 10, 18, 3, 22, tzinfo=timezone(timedelta(hours=8)))
    env["CNE_EVENTS_NOW_EPOCH"] = str(int(later.timestamp()))
    assert _run(EVENTS, env=env).returncode == 0
    assert not calls.exists()


def test_events_quiet_hours_may_wrap_midnight_and_ignore_explicit_dates(tmp_path):
    env, calls = _quiet_env(tmp_path, beijing="23:30", last_ago=900)
    env["CNE_EVENTS_QUIET_HOURS"] = "23-6"

    assert _run(EVENTS, env=env).returncode == 0
    assert not calls.exists()
    # A backfill by date is never thinned.
    assert _run(EVENTS, "2026-10-17", env=env).returncode == 0
    assert calls.exists()


def test_event_templates_ask_for_a_start_offset():
    news = plistlib.loads(NEWS_PLIST.read_bytes())["EnvironmentVariables"]
    events = plistlib.loads(EVENTS_PLIST.read_bytes())["EnvironmentVariables"]
    assert 0 < int(news["CNE_EVENTS_JITTER_SEC"]) < 15 * 60
    assert 0 < int(events["CNE_EVENTS_JITTER_SEC"]) < 60 * 60
    # Overnight thinning belongs to the 15-minute news agent only.
    assert news["CNE_EVENTS_QUIET_HOURS"] == "1-7"
    assert int(news["CNE_EVENTS_QUIET_GAP_SEC"]) > 15 * 60
    assert "CNE_EVENTS_QUIET_HOURS" not in events


def test_an_event_sweep_is_not_blocked_by_the_daily_wrapper(tmp_path):
    """A sweep skipped because the evening batch is running is a sweep lost."""
    cne = _stub_cne(tmp_path)
    calls = tmp_path / "calls"
    env = _stale_env(tmp_path, cne, calls)
    env["CNE_STUB_SLEEP"] = "1"

    daily_lock = tmp_path / "locks" / "daily.lock"
    daily_lock.mkdir(parents=True)
    (daily_lock / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")

    result = _run(EVENTS, env=env)

    assert result.returncode == 0
    assert "skipping" not in result.stdout
    assert _call_args(calls)[:2] == ["run", "events"]


def test_stale_pipeline_forwards_target_date_and_returns_cne_failure(tmp_path):
    cne = _stub_cne(tmp_path)
    calls = tmp_path / "calls"
    env = _stale_env(tmp_path, cne, calls)
    env["CNE_STUB_STATUS"] = "7"

    result = _run(STALE, "2026-08-28", env=env)

    assert result.returncode == 7
    assert _call_args(calls) == [
        "run",
        "daily",
        "--stale-only",
        "--snapshots-only",
        "--config",
        str(tmp_path / "cnequity.toml"),
        "--trade-date",
        "2026-08-28",
    ]
    assert "FAILED" in result.stdout


def test_daily_and_stale_wrappers_share_an_atomic_nonblocking_lock(tmp_path):
    cne = _stub_cne(tmp_path)
    calls = tmp_path / "calls"
    env = _stale_env(tmp_path, cne, calls)
    env["CNE_STUB_SLEEP"] = "1"

    first = _popen(STALE, env)
    lock_dir = tmp_path / "locks" / "daily.lock"
    deadline = time.monotonic() + 2
    while not lock_dir.exists() and time.monotonic() < deadline:
        time.sleep(0.02)

    second = _run(STALE, env=env)
    first_stdout, first_stderr = first.communicate(timeout=5)

    assert lock_dir.exists() is False
    assert second.returncode == 0
    assert "skipping" in second.stdout
    assert first.returncode == 0, first_stdout + first_stderr
    # The second invocation observed the first process and did not submit a
    # duplicate stale-only request.
    assert _call_args(calls).count("--stale-only") == 1


def test_stale_pipeline_recovers_lock_owned_by_dead_process(tmp_path):
    cne = _stub_cne(tmp_path)
    calls = tmp_path / "calls"
    env = _stale_env(tmp_path, cne, calls)
    lock = tmp_path / "locks" / "daily.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(
        json.dumps({"pid": 99999999, "host": host_identity(), "start": "old"}), encoding="utf-8"
    )

    result = _run(STALE, env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _call_args(calls).count("--stale-only") == 1
    assert not lock.exists()


def test_stale_pipeline_does_not_remove_ownerless_lock(tmp_path):
    cne = _stub_cne(tmp_path)
    calls = tmp_path / "calls"
    env = _stale_env(tmp_path, cne, calls)
    lock = tmp_path / "locks" / "daily.lock"
    lock.mkdir(parents=True)

    result = _run(STALE, env=env)

    assert result.returncode == 0
    assert "skipping" in result.stdout
    assert lock.exists()
    assert not calls.exists()


def test_installer_xml_escapes_checkout_path(tmp_path):
    repo = tmp_path / "CN&Equity"
    (repo / "scripts" / "scheduler" / "launchd").mkdir(parents=True)
    (repo / ".venv" / "bin").mkdir(parents=True)
    shutil.copy2(
        ROOT / "scripts" / "scheduler" / "install_scheduler.sh", repo / "scripts" / "scheduler"
    )
    shutil.copy2(
        ROOT / "scripts" / "scheduler" / "scheduler_config.py", repo / "scripts" / "scheduler"
    )
    shutil.copy2(DAILY_PLIST, repo / "scripts" / "scheduler" / "launchd")
    shutil.copy2(STALE_PLIST, repo / "scripts" / "scheduler" / "launchd")
    shutil.copy2(EVENTS_PLIST, repo / "scripts" / "scheduler" / "launchd")
    shutil.copy2(NEWS_PLIST, repo / "scripts" / "scheduler" / "launchd")
    cne = repo / ".venv" / "bin" / "cne"
    cne.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cne.chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uname = fake_bin / "uname"
    uname.write_text("#!/bin/sh\nprintf 'Darwin\\n'\n", encoding="utf-8")
    uname.chmod(0o755)
    launchctl = fake_bin / "launchctl"
    launchctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launchctl.chmod(0o755)
    home = tmp_path / "home"
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "CNE_LAUNCHCTL": str(launchctl),
            "CNE_PYTHON": sys.executable,
        }
    )

    result = _run(repo / "scripts" / "scheduler" / "install_scheduler.sh", env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    daily = plistlib.loads(
        (home / "Library" / "LaunchAgents" / "com.cnequity.daily.plist").read_bytes()
    )
    stale = plistlib.loads(
        (home / "Library" / "LaunchAgents" / "com.cnequity.stale.plist").read_bytes()
    )
    events = plistlib.loads(
        (home / "Library" / "LaunchAgents" / "com.cnequity.events.plist").read_bytes()
    )
    assert daily["ProgramArguments"][-1] == str(
        repo / "scripts" / "scheduler" / "daily_pipeline.sh"
    )
    assert stale["ProgramArguments"][-1] == str(
        repo / "scripts" / "scheduler" / "stale_pipeline.sh"
    )
    assert events["ProgramArguments"][-1] == str(
        repo / "scripts" / "scheduler" / "events_pipeline.sh"
    )


def _stub_cne_failing_groups(tmp_path: Path) -> Path:
    """A stub that fails only the groups named in ``CNE_STUB_FAIL_GROUPS``."""
    path = tmp_path / "cne"
    path.write_text(
        """#!/bin/sh
printf 'argc=%s\\n' "$#" >> "$CNE_CALL_LOG"
for arg in "$@"; do printf '<%s>\\n' "$arg" >> "$CNE_CALL_LOG"; done
group=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "--group" ]; then group="$arg"; fi
  prev="$arg"
done
if [ "$1" = "run" ]; then printf '{"run_id":"stub-%s"}\\n' "$group"; fi
for bad in ${CNE_STUB_FAIL_GROUPS:-}; do
  if [ "$bad" = "$group" ]; then exit 1; fi
done
exit 0
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _daily_env(tmp_path: Path, cne: Path, calls: Path, **extra: str) -> dict[str, str]:
    env = _stale_env(tmp_path, cne, calls)
    env.update(
        {
            "CNE_GROUPS": "core research",
            # Keep the run to the group loop: no probes, no desktop popup, and
            # a data root with no meta/ so backup_meta exits before writing.
            "CNE_SOURCE_HEALTH": "0",
            "CNE_NOTIFY": "0",
            "CNE_DATA_ROOT": str(tmp_path / "lake"),
        }
    )
    env.update(extra)
    return env


def _run_daily(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return _run(DAILY, env=env)


def test_daily_pipeline_fails_hard_when_a_gate_group_fails(tmp_path):
    """The gate decides whether anyone gets paged, and nothing exercised it."""
    cne = _stub_cne_failing_groups(tmp_path)
    env = _daily_env(tmp_path, cne, tmp_path / "calls", CNE_STUB_FAIL_GROUPS="core")

    result = _run_daily(env)

    assert result.returncode == 1
    assert "GATE FAILED: core" in result.stdout
    # A failing gate group must not stop the others from getting their turn.
    assert "group research OK" in result.stdout


def test_daily_pipeline_keeps_a_soft_group_failure_warn_only(tmp_path):
    """Overseas EastMoney lag must not paint an otherwise good day red."""
    cne = _stub_cne_failing_groups(tmp_path)
    env = _daily_env(tmp_path, cne, tmp_path / "calls", CNE_STUB_FAIL_GROUPS="research")

    result = _run_daily(env)

    assert result.returncode == 0
    assert "warn-only" in result.stdout
    assert "research" in result.stdout


def test_daily_pipeline_can_be_told_to_fail_on_a_soft_group(tmp_path):
    cne = _stub_cne_failing_groups(tmp_path)
    env = _daily_env(
        tmp_path,
        cne,
        tmp_path / "calls",
        CNE_STUB_FAIL_GROUPS="research",
        CNE_SOFT_FAIL_OK="0",
    )

    result = _run_daily(env)

    assert result.returncode == 1
    assert "EM/soft FAILED: research" in result.stdout


def test_daily_pipeline_exits_0_when_every_group_succeeds(tmp_path):
    cne = _stub_cne_failing_groups(tmp_path)
    env = _daily_env(tmp_path, cne, tmp_path / "calls")

    result = _run_daily(env)

    assert result.returncode == 0
    assert "DONE ok" in result.stdout


def test_health_notify_honours_the_same_cne_override_as_the_pipeline(tmp_path):
    """`daily_pipeline.sh` runs this script and honours CNE_BIN; this one
    hardcoded the repo venv, so an override left the health gate probing a
    different — or absent — binary while the pipeline used the real one.
    """
    cne = _stub_cne_failing_groups(tmp_path)
    calls = tmp_path / "calls"
    env = _daily_env(tmp_path, cne, calls)

    result = _run(ROOT / "scripts" / "scheduler" / "health_notify.sh", env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout
    # It really went through the stub, not some other cne on the machine.
    assert "audit" in _call_args(calls)
    assert "--datasets" in _call_args(calls)


def _soft_fail_env(tmp_path: Path, cne: Path, calls: Path) -> dict[str, str]:
    env = _stale_env(tmp_path, cne, calls)
    env.update(
        {
            # One soft group, no gate group, so the run is warn-only by default.
            "CNE_GROUPS": "capital",
            "CNE_GATE_GROUPS": "core",
            "CNE_SOFT_FAIL_OK": "1",
            "CNE_STALE_RETRY": "0",
            "CNE_SOURCE_HEALTH": "0",
            "CNE_SOFT_STREAK_DIR": str(tmp_path / "streak"),
            "CNE_STUB_STATUS": "1",  # the group fails
        }
    )
    return env


def test_a_soft_group_down_for_three_days_stops_being_warn_only(tmp_path):
    """One bad day is noise; three in a row is the outage the runbook records.

    `CNE_SOFT_FAIL_OK=1` made every soft failure exit 0, which is how a group
    stayed down for three days without anyone noticing. Persistence, not a
    single result, is what should turn the run red.
    """
    cne = _stub_cne(tmp_path)
    calls = tmp_path / "calls.log"
    env = _soft_fail_env(tmp_path, cne, calls)
    env["CNE_SOFT_FAIL_MAX_DAYS"] = "3"

    first = _run(DAILY, "2026-09-11", env=env)
    repeated = _run(DAILY, "2026-09-11", env=env)
    assert repeated.returncode == 0
    second = _run(DAILY, "2026-09-14", env=env)
    assert first.returncode == 0, "one soft failure stays warn-only"
    assert second.returncode == 0, "two is still within tolerance"

    third = _run(DAILY, "2026-09-15", env=env)
    assert third.returncode == 1, "a soft group down three days is an outage"

    log = (tmp_path / "logs").glob("daily-*.log")
    text = "\n".join(p.read_text(encoding="utf-8") for p in log)
    assert "SOFT GROUP DOWN" in text
    assert "capital:3d" in text


def test_a_recovered_soft_group_clears_its_streak(tmp_path):
    cne = _stub_cne(tmp_path)
    calls = tmp_path / "calls.log"
    env = _soft_fail_env(tmp_path, cne, calls)
    env["CNE_SOFT_FAIL_MAX_DAYS"] = "2"

    assert _run(DAILY, env=env).returncode == 0
    # The source comes back before the threshold is reached.
    recovered = dict(env, CNE_STUB_STATUS="0")
    assert _run(DAILY, env=recovered).returncode == 0
    assert not (tmp_path / "streak" / "capital").exists()

    # The streak restarts from zero rather than resuming where it left off.
    assert _run(DAILY, env=env).returncode == 0


def test_escalation_can_be_disabled(tmp_path):
    cne = _stub_cne(tmp_path)
    calls = tmp_path / "calls.log"
    env = _soft_fail_env(tmp_path, cne, calls)
    env["CNE_SOFT_FAIL_MAX_DAYS"] = "0"

    for _ in range(4):
        assert _run(DAILY, env=env).returncode == 0


def test_health_notify_runs_the_whole_lake_audit_only_on_its_weekly_day(tmp_path):
    """`cne audit --full` opens every curated Parquet file.

    On a 25 GB lake that is I/O bound for hours, and it ran on every trading
    day — making the health check, not ingestion, the cost of the pipeline.
    """
    cne = _stub_cne(tmp_path)
    calls = tmp_path / "calls"
    env = _daily_env(tmp_path, cne, calls)

    env["CNE_FULL_AUDIT_DOW"] = "0"  # never
    _run(ROOT / "scripts" / "scheduler" / "health_notify.sh", env=env)
    assert "--full" not in calls.read_text()
    assert "audit" in calls.read_text()

    calls.unlink()
    env["CNE_FULL_AUDIT_DOW"] = "always"
    _run(ROOT / "scripts" / "scheduler" / "health_notify.sh", env=env)
    assert "--full" in calls.read_text()


def test_health_notify_scopes_the_freshness_gate_to_the_scheduled_groups(tmp_path):
    """The gate and the scheduler have to agree on what this host fetches.

    A core-only plist leaves twenty-odd datasets that no job here ever runs, so
    unscoped the gate failed every day — 21 to 25 stale on 2026-09-12/13/14 —
    and the daily notification became something to dismiss. CNE_GROUPS is the
    same variable the plist and the pipeline use, so the two cannot drift.
    """
    cne = _stub_cne_failing_groups(tmp_path)
    calls = tmp_path / "calls"
    env = _daily_env(tmp_path, cne, calls)
    env["CNE_GROUPS"] = "core"

    result = _run(ROOT / "scripts" / "scheduler" / "health_notify.sh", env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    args = _call_args(calls)
    assert "--datasets" in args
    assert "--groups" in args
    assert "core" in args


def test_health_notify_passes_no_scope_when_no_groups_are_configured(tmp_path):
    """Without CNE_GROUPS there is nothing to scope to, and the gate must keep
    its original meaning: any stale dataset fails."""
    cne = _stub_cne_failing_groups(tmp_path)
    calls = tmp_path / "calls"
    env = _daily_env(tmp_path, cne, calls)
    env.pop("CNE_GROUPS", None)

    result = _run(ROOT / "scripts" / "scheduler" / "health_notify.sh", env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "--groups" not in _call_args(calls)


def test_health_notify_titles_a_freshness_miss_as_lag_not_an_anomaly(tmp_path):
    """ "数据异常" over a pure freshness miss is the kind of wrong that costs an
    alert its meaning: nothing was anomalous, a scheduled group had not run."""
    script = (ROOT / "scripts" / "scheduler" / "health_notify.sh").read_text(encoding="utf-8")

    assert 'title="cnequity 数据滞后"' in script
    assert 'title="cnequity 数据异常"' in script
    # The anomaly title is reserved for the audit/health arm of the gate.
    assert "*health*|*error*) title=" in script


def test_stale_wrapper_forwards_host_groups(tmp_path):
    calls = tmp_path / "calls"
    env = _stale_env(tmp_path, _stub_cne(tmp_path), calls)
    env["CNE_GROUPS"] = "core capital"
    assert _run(STALE, env=env).returncode == 0
    args = _call_args(calls)
    assert args[args.index("--groups") + 1] == "core capital"


def test_daily_pipeline_fails_when_quality_gate_fails_after_successful_fetch(tmp_path):
    calls = tmp_path / "calls"
    env = _daily_env(tmp_path, _stub_cne(tmp_path), calls)
    env["CNE_STUB_CHECK_STATUS"] = "1"
    result = _run(DAILY, env=env)
    assert result.returncode == 1
    assert "HEALTH GATE FAILED" in result.stdout


def test_daily_health_scopes_freshness_to_scheduled_hard_groups(tmp_path):
    calls = tmp_path / "calls"
    env = _daily_env(tmp_path, _stub_cne(tmp_path), calls)
    assert _run(DAILY, env=env).returncode == 0
    args = _call_args(calls)
    assert args[args.index("--groups") + 1] == "core"


def test_daily_health_audits_every_group_in_one_call(tmp_path):
    calls = tmp_path / "calls"
    env = _daily_env(tmp_path, _stub_cne_failing_groups(tmp_path), calls)
    env["CNE_FULL_AUDIT_DOW"] = "0"
    result = _run(DAILY, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    args = _call_args(calls)
    assert args.count("audit") == 1
    assert args[args.index("audit") + 1 : args.index("audit") + 5] == [
        "--run-id",
        "stub-core",
        "--run-id",
        "stub-research",
    ]


def test_non_trading_skip_does_not_erase_a_soft_outage(tmp_path):
    cne = _stub_cne(tmp_path)
    calls = tmp_path / "calls"
    env = _soft_fail_env(tmp_path, cne, calls)
    env["CNE_SOFT_FAIL_MAX_DAYS"] = "2"
    assert _run(DAILY, "2026-09-11", env=env).returncode == 0
    cne.write_text('#!/bin/sh\nprintf \'{"status": "skipped_non_trading_day"}\\n\'\nexit 0\n')
    assert _run(DAILY, "2026-09-12", env=env).returncode == 0
    assert (tmp_path / "streak/capital").read_text().strip() == "2026-09-11 1"
    _stub_cne(tmp_path)
    assert _run(DAILY, "2026-09-14", env=env).returncode == 1


@pytest.mark.parametrize("override, expected", [(None, "unset"), ("0", "0"), ("1", "1")])
def test_stale_pass_preserves_the_deployments_push2_policy(tmp_path, override, expected):
    cne = _stub_cne(tmp_path)
    cne.write_text(
        '#!/bin/sh\nprintf "%s" "${CNE_PUSH2_PAUSED-unset}" > "$CNE_CALL_LOG"\n',
        encoding="utf-8",
    )
    calls = tmp_path / "calls"
    env = _stale_env(tmp_path, cne, calls)
    env.pop("CNE_PUSH2_PAUSED", None)
    if override is not None:
        env["CNE_PUSH2_PAUSED"] = override
    result = _run(STALE, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls.read_text(encoding="utf-8") == expected


def test_daily_pipeline_reports_a_resting_group_as_skipped(tmp_path):
    cne = _stub_cne(tmp_path)
    cne.write_text(
        """#!/bin/sh
if [ "$1 $2" = "run daily" ]; then
  printf '%s\n' '{"status": "skipped_not_scheduled"}'
fi
exit 0
""",
        encoding="utf-8",
    )
    streak = tmp_path / "streak"
    streak.mkdir()
    prior = "2026-09-23 2\n"
    (streak / "research").write_text(prior, encoding="utf-8")
    env = _daily_env(
        tmp_path,
        cne,
        tmp_path / "calls",
        CNE_GROUPS="research",
        CNE_SOFT_STREAK_DIR=str(streak),
        CNE_SOFT_FAIL_MAX_DAYS="3",
    )
    result = _run(DAILY, "2026-09-24", env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "research: SKIPPED" in result.stdout
    assert "research: OK" not in result.stdout
    assert (streak / "research").read_text(encoding="utf-8") == prior
