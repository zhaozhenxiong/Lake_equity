"""OS adapters: render real definitions, mutate only fake scheduler surfaces."""

from __future__ import annotations

import plistlib
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from cnequity.serve.ops.catalog import OpsError
from cnequity.serve.ops.scheduler_backend import NS, SchedulerBackend, run_command


@pytest.fixture
def backend(tmp_path, monkeypatch):
    value = SchedulerBackend(
        tmp_path / "中文 & 50% config.toml", tmp_path / "lake", working_directory=str(tmp_path)
    )
    value.plist = tmp_path / "agents" / f"{value.name}.plist"
    value.control_lock = tmp_path / "user-control.lock"
    monkeypatch.setattr(value, "available", lambda: True)
    return value


def _as_linux_cron(backend) -> None:
    """Cron tests render a POSIX command line. Windows tmp paths are not valid there."""
    backend.platform = "linux"
    backend.working_directory = "/tmp/cne work"
    backend.argv = [
        "/usr/bin/python",
        "-X",
        "utf8",
        "-m",
        "cnequity.serve.ops.scheduled",
        "--config",
        "/tmp/中文 & 50% config.toml",
        "--scheduler-lock-dir",
        "/tmp/cne-locks",
    ]


def test_cron_preserves_other_entries_and_escapes_paths(backend, monkeypatch):
    _as_linux_cron(backend)
    table = ["# user job\nMAILTO=someone\n0 0 * * * /usr/bin/true\n"]
    original = table[0]
    monkeypatch.setattr(backend, "_crontab", lambda: table[0])
    calls = []

    def run(argv, *, input=None):
        calls.append(argv)
        assert argv == ["crontab", "-"]
        table[0] = input.decode()
        return b""

    monkeypatch.setattr("cnequity.serve.ops.scheduler_backend.run_command", run)
    artifact = backend.render()
    assert r"50\%" in artifact and "'" in artifact
    assert "cd " in artifact and " && exec " in artifact
    table[0] = original + artifact.replace("* * * * * ", "*/10 * * * * ", 1)
    assert backend.observe()["owned"] and not backend.observe()["matches"]
    backend.apply(True, artifact)
    assert table[0].startswith(original)
    assert backend.observe()["matches"]
    backend.apply(True, artifact)
    assert table[0].count(f"# {backend.name}") == 1
    backend.apply(False, "")
    assert table[0] == original
    assert not backend.observe()["installed"]


@pytest.mark.usefixtures("chinese_output")
def test_cron_refuses_permission_failures_and_unknown_owned_entry(backend, monkeypatch):
    _as_linux_cron(backend)
    monkeypatch.setattr(
        "cnequity.serve.ops.scheduler_backend.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, b"", b"permission denied"),
    )
    with pytest.raises(OpsError, match="权限"):
        backend._crontab()
    monkeypatch.setattr(
        "cnequity.serve.ops.scheduler_backend.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, b"", b"no crontab for test"),
    )
    assert backend._crontab() == ""
    monkeypatch.setattr(backend, "_crontab", lambda: f"* * * * * /other-command # {backend.name}\n")
    with pytest.raises(OpsError, match="不符"):
        backend.apply(True, backend.render())


def test_launchd_definition_and_reload_only_owned_agent(backend, monkeypatch):
    backend.platform = "darwin"
    monkeypatch.setattr(
        "cnequity.serve.ops.scheduler_backend.os.getuid", lambda: 501, raising=False
    )
    active = [False]
    calls = []
    monkeypatch.setattr(
        "cnequity.serve.ops.scheduler_backend.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0 if active[0] else 1),
    )

    def run(argv, **_kwargs):
        calls.append(argv)
        if argv[1] == "bootout":
            active[0] = False
        elif argv[1] == "bootstrap":
            active[0] = True
        return b""

    monkeypatch.setattr("cnequity.serve.ops.scheduler_backend.run_command", run)
    artifact = backend.render()
    parsed = plistlib.loads(artifact.encode())
    assert parsed["ProgramArguments"] == backend.argv
    assert parsed["StartCalendarInterval"] == [{"Minute": n} for n in range(60)]
    old = {**parsed, "StartCalendarInterval": [{"Minute": n} for n in range(0, 60, 10)]}
    backend.plist.parent.mkdir()
    backend.plist.write_bytes(plistlib.dumps(old))
    assert backend.observe()["owned"] and not backend.observe()["matches"]
    backend.apply(True, artifact)
    assert backend.observe()["active"]
    backend.apply(True, artifact)
    backend.apply(False, "")
    assert not backend.plist.exists()
    assert all(backend.name in " ".join(command) or command[1] == "bootstrap" for command in calls)


def test_windows_definition_registration_and_removal(backend, monkeypatch):
    backend.platform = "win32"
    saved = [""]
    calls = []
    monkeypatch.setattr(backend, "_windows_xml", lambda: saved[0])

    def run(argv, **_kwargs):
        calls.append(argv)
        if argv[0] == "whoami":
            return b'"test","S-1-5-21-123-456-789-1001"'
        if "/Create" in argv:
            saved[0] = Path(argv[argv.index("/XML") + 1]).read_text(encoding="utf-16")
        elif "/Delete" in argv:
            saved[0] = ""
        return b""

    monkeypatch.setattr("cnequity.serve.ops.scheduler_backend.run_command", run)
    artifact = backend.render()
    root = ET.fromstring(artifact)
    assert root.findtext(f".//{{{NS}}}LogonType") == "InteractiveToken"
    assert root.findtext(f".//{{{NS}}}RunLevel") == "LeastPrivilege"
    assert root.findtext(f".//{{{NS}}}Interval") == "PT1M"
    assert root.findtext(f".//{{{NS}}}Arguments") == subprocess.list2cmdline(backend.argv[1:])
    saved[0] = artifact.replace("PT1M", "PT10M")
    assert backend.observe()["owned"] and not backend.observe()["matches"]
    backend.apply(True, artifact)
    assert backend.observe()["active"] and backend.observe()["matches"]
    backend.apply(False, "")
    assert not backend.observe()["installed"]
    assert any("/Create" in call for call in calls) and any("/Delete" in call for call in calls)


def test_native_command_timeout_and_errors_do_not_leak_output(monkeypatch):
    def fail(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("secret command", 15)

    monkeypatch.setattr("cnequity.serve.ops.scheduler_backend.subprocess.run", fail)
    with pytest.raises(OpsError, match="TimeoutExpired"):
        run_command(["crontab", "-l"])
    monkeypatch.setattr(
        "cnequity.serve.ops.scheduler_backend.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 1, b"token=secret", b"password=secret"
        ),
    )
    with pytest.raises(OpsError) as caught:
        run_command(["crontab", "-l"])
    assert "secret" not in str(caught.value)


def test_corrupt_plist_is_reported_without_mutation(backend, monkeypatch):
    backend.platform = "darwin"
    backend.plist.parent.mkdir()
    backend.plist.write_text("<plist><dict>not closed")
    report = backend.observe()
    assert report["error"] and report["installed"] is None


def test_explicit_scheduler_lock_directory_is_in_timer_arguments(tmp_path):
    backend = SchedulerBackend(
        tmp_path / "config.toml",
        tmp_path / "meta",
        scheduler_lock_dir=str(tmp_path / "shared-locks"),
    )
    assert backend.argv[-2:] == ["--scheduler-lock-dir", str(tmp_path / "shared-locks")]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Task Scheduler COM schema")
def test_native_windows_xml_validates_without_registering_any_task(tmp_path):
    backend = SchedulerBackend(tmp_path / "中文 配置.toml", tmp_path / "lake")
    path = tmp_path / "task.xml"
    path.write_text(backend.render(), encoding="utf-16")
    # NewTask is an in-memory definition. No RegisterTask* or schtasks /Create.
    escaped = str(path).replace("'", "''")
    script = (
        "$ErrorActionPreference='Stop'; $s=New-Object -ComObject Schedule.Service; "
        "$s.Connect(); $t=$s.NewTask(0); "
        f"$t.XmlText=(Get-Content -LiteralPath '{escaped}' -Raw); $t.XmlText"
    )
    result = run_command(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
    assert result


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS plist syntax")
def test_native_plist_validates_without_loading_any_agent(tmp_path):
    backend = SchedulerBackend(tmp_path / "中文 配置.toml", tmp_path / "lake")
    path = tmp_path / "job.plist"
    path.write_text(backend.render(), encoding="utf-8")
    assert b"OK" in run_command(["plutil", "-lint", str(path)])
