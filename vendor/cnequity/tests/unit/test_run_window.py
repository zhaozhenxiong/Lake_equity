"""Scheduled runs: once per session, after a Beijing run_at, whatever the host timezone."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import date, datetime, time
from pathlib import Path

import pytest

from cnequity.config import Config, load_config
from cnequity.config.bootstrap import path_for_toml
from cnequity.config.loader import validate_config
from cnequity.orchestrator.run_window import (
    due_session,
    is_done,
    mark_done,
    pending_session,
)

ROOT = Path(__file__).resolve().parents[2]
_AT = time(17, 30)


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


@pytest.mark.parametrize(
    ("now", "session"),
    [
        # Thursday 9-24: 17:40 Beijing is due, 17:20 is not (9-23 already ran by then
        # would be the previous session — but 9-24's open has passed, so nothing).
        ("2026-09-24T09:40:00+00:00", date(2026, 9, 24)),
        ("2026-09-24T09:20:00+00:00", None),
        # A host in Helsinki (UTC+3 now, UTC+2 after 10-25) or New York asks the
        # same question in UTC; the answer only depends on Beijing time.
        ("2026-09-24T12:40:00-04:00", date(2026, 9, 24)),
        # Saturday: still 9-24's window (9-25 was the Mid-Autumn close).
        ("2026-09-26T12:00:00+00:00", date(2026, 9, 24)),
        # Monday 08:30 Beijing, before the open: a Mac that slept all weekend
        # still owes 9-24.
        ("2026-09-28T00:30:00+00:00", date(2026, 9, 24)),
        # Monday 10:00 Beijing: 9-24's window closed at the 09:15 open.
        ("2026-09-28T02:00:00+00:00", None),
    ],
)
def test_the_due_session_depends_only_on_beijing_time(now, session):
    assert due_session(_utc(now), _AT) == session


def _cfg(tmp_path) -> Config:
    return Config(data_root=tmp_path / "data")


def test_a_session_runs_once(tmp_path):
    cfg = _cfg(tmp_path)
    now = _utc("2026-09-24T09:40:00+00:00")
    assert pending_session(cfg, "daily", now) == date(2026, 9, 24)
    mark_done(cfg.meta_root, "daily", date(2026, 9, 24))
    assert pending_session(cfg, "daily", now) is None


def test_the_catch_up_waits_for_the_days_run(tmp_path):
    cfg = _cfg(tmp_path)
    now = _utc("2026-09-24T13:10:00+00:00")  # 21:10 Beijing, past stale run_at 21:00
    assert pending_session(cfg, "stale", now) is None
    mark_done(cfg.meta_root, "daily", date(2026, 9, 24))
    assert pending_session(cfg, "stale", now) == date(2026, 9, 24)


def test_a_panel_run_does_not_count_as_the_scheduled_day(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(cfg.manifest_path) as conn:
        conn.execute("CREATE TABLE ingestion_runs (job_name TEXT, metadata_json TEXT)")
        conn.execute(
            "INSERT INTO ingestion_runs VALUES (?, ?)",
            (
                "daily:core",
                json.dumps(
                    {"trade_date": "2026-09-24", "launch": {"id": "abc", "source": "serve"}}
                ),
            ),
        )
    assert not is_done(cfg.meta_root, "daily", date(2026, 9, 24), cfg)
    with sqlite3.connect(cfg.manifest_path) as conn:
        conn.execute(
            "INSERT INTO ingestion_runs VALUES (?, ?)",
            ("daily:capital", json.dumps({"trade_date": "2026-09-24"})),
        )
    assert is_done(cfg.meta_root, "daily", date(2026, 9, 24), cfg)


def test_a_run_already_in_the_manifest_counts_as_done(tmp_path):
    """Sessions from before the markers, and manual runs, are not run again."""
    cfg = _cfg(tmp_path)
    cfg.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(cfg.manifest_path) as conn:
        conn.execute("CREATE TABLE ingestion_runs (job_name TEXT, metadata_json TEXT)")
        conn.execute(
            "INSERT INTO ingestion_runs VALUES (?, ?)",
            ("daily:core", json.dumps({"trade_date": "2026-09-24"})),
        )
    assert is_done(cfg.meta_root, "daily", date(2026, 9, 24), cfg)
    assert not is_done(cfg.meta_root, "stale", date(2026, 9, 24), cfg)
    assert pending_session(cfg, "daily", _utc("2026-09-24T09:40:00+00:00")) is None


def test_run_at_is_configurable_and_validated(tmp_path):
    path = tmp_path / "cnequity.toml"
    path.write_text(
        f"""[data]
root = "{path_for_toml(tmp_path / "data")}"
[job.daily]
run_at = "16:15"
[job.stale]
run_at = "22:00"
""",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert (cfg.daily_run_at, cfg.stale_run_at) == ("16:15", "22:00")
    assert not [e for e in validate_config(cfg) if "run_at" in e]
    cfg.daily_run_at = "25:00"
    assert any("job.daily.run_at" in e for e in validate_config(cfg))


def test_the_gate_script_checks_and_marks(tmp_path):
    path = tmp_path / "cnequity.toml"
    path.write_text(f'[data]\nroot = "{path_for_toml(tmp_path / "data")}"\n', encoding="utf-8")
    gate = ROOT / "scripts" / "scheduler" / "scheduler_gate.py"
    mark = subprocess.run(
        [sys.executable, str(gate), "mark", "daily", "2026-09-24", "--config", str(path)],
        capture_output=True,
        text=True,
    )
    assert mark.returncode == 0, mark.stderr
    assert (tmp_path / "data" / "meta" / "state" / "scheduler" / "daily-2026-09-24.done").exists()
    check = subprocess.run(
        [sys.executable, str(gate), "check", "nope", "--config", str(path)],
        capture_output=True,
        text=True,
    )
    assert check.returncode == 2  # argparse rejects an unknown job


# ---- the pipelines honour the gate --------------------------------------------------


def _fake_python(tmp_path: Path, check_rc: int, session: str = "2026-09-24") -> Path:
    """A stand-in interpreter that answers scheduler_gate.py and logs marks."""
    path = tmp_path / "python"
    path.write_text(
        f"""#!/bin/sh
case "$2" in
  check) [ {check_rc} -eq 0 ] && echo {session}; exit {check_rc} ;;
  mark) echo "mark $3 $4" >> "{tmp_path}/marks"; exit 0 ;;
esac
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _env(tmp_path, python: Path) -> dict[str, str]:
    import os

    cne = tmp_path / "cne"
    cne.write_text(
        '#!/bin/sh\nfor arg in "$@"; do printf \'<%s>\\n\' "$arg" >> "$CNE_CALL_LOG"; done\n',
        encoding="utf-8",
    )
    cne.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "CNE_BIN": str(cne),
            "CNE_CALL_LOG": str(tmp_path / "calls"),
            "CNE_CONFIG": str(tmp_path / "cnequity.toml"),
            "CNE_LOG_DIR": str(tmp_path / "logs"),
            "CNE_SCHEDULER_LOCK_DIR": str(tmp_path / "locks"),
            "CNE_DATA_ROOT": str(tmp_path / "lake"),
            "CNE_BACKUP_DIR": str(tmp_path / "backups"),
            "CNE_NOTIFY": "0",
            "CNE_SOURCE_HEALTH": "0",
            "CNE_SCHEDULED": "1",
            "CNE_PYTHON": str(python),
            "CNE_GROUPS": "core",
        }
    )
    return env


@pytest.mark.skipif(sys.platform == "win32", reason="Unix shell scripts")
@pytest.mark.parametrize("script", ["daily_pipeline.sh", "stale_pipeline.sh"])
def test_a_wake_up_that_is_not_due_does_nothing(tmp_path, script):
    env = _env(tmp_path, _fake_python(tmp_path, check_rc=3))
    done = subprocess.run(
        [str(ROOT / "scripts" / "scheduler" / script)], env=env, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    assert not (tmp_path / "calls").exists()
    assert not (tmp_path / "marks").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Unix shell scripts")
@pytest.mark.parametrize(
    ("script", "job"), [("daily_pipeline.sh", "daily"), ("stale_pipeline.sh", "stale")]
)
def test_a_due_wake_up_runs_that_session_once(tmp_path, script, job):
    env = _env(tmp_path, _fake_python(tmp_path, check_rc=0))
    subprocess.run(
        [str(ROOT / "scripts" / "scheduler" / script)], env=env, capture_output=True, text=True
    )
    calls = (tmp_path / "calls").read_text(encoding="utf-8")
    assert "<--trade-date>\n<2026-09-24>" in calls
    assert (tmp_path / "marks").read_text().split() == ["mark", job, "2026-09-24"]
