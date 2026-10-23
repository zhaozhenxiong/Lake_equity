from pathlib import Path
import runpy

import lake_equity.cli


def test_spawned_worker_does_not_restart_the_server(monkeypatch):
    calls = []
    monkeypatch.setattr(lake_equity.cli, "main", lambda: calls.append("main"))
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "run.py"), run_name="__mp_main__")
    assert calls == []


def test_direct_entrypoint_starts_the_cli(monkeypatch):
    calls = []
    monkeypatch.setattr(lake_equity.cli, "main", lambda: calls.append("main"))
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "run.py"), run_name="__main__")
    assert calls == ["main"]
