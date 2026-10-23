"""Regeneration must preserve host policy and expose drift without installing jobs."""

import importlib.util
import os
import plistlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/scheduler/scheduler_config.py"
spec = importlib.util.spec_from_file_location("scheduler_config", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_renderer_preserves_host_groups_cadence_and_event_scope(tmp_path, monkeypatch):
    monkeypatch.delenv("CNE_LANG", raising=False)
    old = {
        "StartCalendarInterval": {"Hour": 11, "Minute": 15},
        "EnvironmentVariables": {
            "CNE_GROUPS": "core",
            "CNE_SOURCE_VANTAGE": "overseas",
            "CNE_CONFIG": "/my/config.toml",
        },
    }
    (tmp_path / "com.cnequity.daily.plist").write_bytes(plistlib.dumps(old))
    event = {"StartInterval": 900, "EnvironmentVariables": {"CNE_EVENTS_GROUP": "disclosures"}}
    (tmp_path / "com.cnequity.events.plist").write_bytes(plistlib.dumps(event))
    jobs = module.render_jobs(ROOT, tmp_path, groups=None, vantage=None)
    assert jobs["com.cnequity.daily"]["EnvironmentVariables"] == {
        **old["EnvironmentVariables"],
        "CNE_STALE_RETRY": "0",
        "CNE_SCHEDULED": "1",
    }
    # The run time lives in the config ([job.daily] run_at, Beijing time); an
    # old host-local launch time must not survive regeneration.
    assert jobs["com.cnequity.daily"]["StartCalendarInterval"] == {"Minute": 7}
    assert jobs["com.cnequity.stale"]["EnvironmentVariables"]["CNE_SCHEDULED"] == "1"
    assert jobs["com.cnequity.stale"]["EnvironmentVariables"]["CNE_GROUPS"] == "core"
    assert jobs["com.cnequity.stale"]["EnvironmentVariables"]["CNE_CONFIG"] == "/my/config.toml"
    assert jobs["com.cnequity.events"]["StartInterval"] == 900
    assert "StartCalendarInterval" not in jobs["com.cnequity.events"]
    assert jobs["com.cnequity.events"]["EnvironmentVariables"]["CNE_EVENTS_GROUP"] == "disclosures"
    changed = module.render_jobs(ROOT, tmp_path, groups="core,capital", vantage="cn")
    assert changed["com.cnequity.daily"]["EnvironmentVariables"]["CNE_GROUPS"] == "core capital"


def test_the_installing_shells_language_reaches_the_agents(tmp_path, monkeypatch):
    """launchd gives a job no LANG; without this a Chinese setup reports in English."""
    monkeypatch.setenv("CNE_LANG", "zh_CN")
    jobs = module.render_jobs(ROOT, tmp_path, groups="core", vantage=None)
    for label in ("daily", "stale", "events", "events-news"):
        assert jobs[f"com.cnequity.{label}"]["EnvironmentVariables"]["CNE_LANG"] == "zh_CN"
    # An operator's own choice in the installed plist wins over the shell.
    old = {"EnvironmentVariables": {"CNE_LANG": "en"}}
    (tmp_path / "com.cnequity.events.plist").write_bytes(plistlib.dumps(old))
    again = module.render_jobs(ROOT, tmp_path, groups="core", vantage=None)
    assert again["com.cnequity.events"]["EnvironmentVariables"]["CNE_LANG"] == "en"


def test_check_and_dry_run_do_not_install_or_overwrite_host_files(tmp_path):
    dest = tmp_path / "installed"
    preview = tmp_path / "preview"
    env = dict(
        os.environ,
        CNE_SCHEDULER_DEST_DIR=str(dest),
        CNE_GROUPS="core",
        CNE_SOURCE_VANTAGE="overseas",
        CNE_LAUNCHCTL="/must/not/run",
    )

    def run(*args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    assert run("--check").returncode == 1
    assert not dest.exists()
    result = run("--dry-run", str(preview))
    assert result.returncode == 0, result.stderr
    assert not dest.exists()
    preview.rename(dest)
    assert run("--check").returncode == 0
    before = (dest / "com.cnequity.daily.plist").read_bytes()
    assert run("--dry-run", str(dest)).returncode == 1
    assert (dest / "com.cnequity.daily.plist").read_bytes() == before


def test_new_install_has_six_groups_and_rejects_empty_override(tmp_path):
    import pytest

    jobs = module.render_jobs(ROOT, tmp_path, groups=None, vantage=None)
    assert jobs["com.cnequity.daily"]["EnvironmentVariables"]["CNE_GROUPS"] == module.DEFAULT_GROUPS
    with pytest.raises(ValueError, match="non-empty"):
        module.render_jobs(ROOT, tmp_path, groups="", vantage=None)


def _noop_launchctl(directory: Path) -> Path:
    """A load/unload stub CreateProcess can actually execute.

    ``/usr/bin/true`` is not a file on Windows, and native Python does not
    translate the Git Bash path, so pointing ``CNE_LAUNCHCTL`` there made the
    installer fail with WinError 2 on the only job that runs this test on
    Windows CI.
    """
    stub = directory / "launchctl.py"
    stub.write_text("import sys\nraise SystemExit(0)\n", encoding="utf-8")
    return stub


def test_python_launchctl_stub_is_run_with_this_interpreter(tmp_path, monkeypatch):
    stub = _noop_launchctl(tmp_path)
    monkeypatch.setenv("CNE_LAUNCHCTL", str(stub))
    assert module._launchctl_command() == [sys.executable, str(stub)]
    monkeypatch.setenv("CNE_LAUNCHCTL", "launchctl")
    assert module._launchctl_command() == ["launchctl"]


def test_stale_at_is_retired_and_an_old_catch_up_time_is_replaced(tmp_path):
    dest = tmp_path / "installed"
    dest.mkdir()
    old = {"EnvironmentVariables": {"CNE_GROUPS": "core", "CNE_SOURCE_VANTAGE": "overseas"}}
    daily = dest / "com.cnequity.daily.plist"
    daily.write_bytes(plistlib.dumps(old))
    before = daily.read_bytes()
    stale = dest / "com.cnequity.stale.plist"
    stale.write_bytes(plistlib.dumps({"StartCalendarInterval": {"Hour": 23, "Minute": 0}}))
    env = dict(
        os.environ,
        CNE_SCHEDULER_DEST_DIR=str(dest),
        CNE_LAUNCHCTL=str(_noop_launchctl(tmp_path)),
    )
    env.pop("CNE_GROUPS", None)
    env.pop("CNE_SOURCE_VANTAGE", None)

    def run(*args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True
        )

    retired = run("--stale-only", "--stale-at", "23:00")
    assert retired.returncode == 2
    assert "run_at" in retired.stderr
    result = run("--stale-only")
    assert result.returncode == 0, result.stderr
    assert daily.read_bytes() == before
    assert not (dest / "com.cnequity.events.plist").exists()
    job = plistlib.loads(stale.read_bytes())
    assert job["StartCalendarInterval"] == {"Minute": 37}
    assert job["EnvironmentVariables"]["CNE_GROUPS"] == "core"
    assert job["EnvironmentVariables"]["CNE_SCHEDULED"] == "1"
    assert job["RunAtLoad"] is False
    assert run("--check", "--stale-only").returncode == 0
