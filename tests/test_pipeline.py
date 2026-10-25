"""One-click pipeline contract tests (isolated lake; never runs real collection)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from lake_equity import pipeline
from lake_equity.pipeline import PipelineService, run_data_update

TARGET = "2026-10-23"
SESSIONS = ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"]
REQUIRED = ["daily_bars", "trading_calendar"]
EXTRA = ["instruments", "trading_status", "corporate_actions", "margin_trading",
         "announcement_index", "top_holders", "futures_minute_bars"]
REGISTERED = REQUIRED + EXTRA

RELEASE = {
    "release_id": "release-" + "a" * 32,
    "name": "test default",
    "model_id": "model-" + "b" * 32,
    "series_id": "series-" + "c" * 32,
    "study_id": "study-" + "d" * 32,
    "information_cutoff": "2026-10-21",
    "created_at": "2026-01-01T00:00:00+00:00",
    "read_ref": "e" * 64,
    "risk": {"top_k": 10, "initial_cash": 1_000_000, "max_total_weight": 0.8,
             "max_stock_weight": 0.1, "rebalance_sessions": 5},
    "universe": {"policy": "sh_sz_a_v1"},
    "qualification": {"research_only": True, "strict_history_eligible": False},
}

CONFIG = """\
[data]
root = "var/lake"

[[job.daily.waves]]
name = "reference"
steps = ["instruments", "trading_status"]

[[job.daily.waves]]
name = "corp_actions_to_bars"
steps = ["corporate_actions", "daily_bars"]

[job.daily.groups.core]
steps = ["instruments", "trading_status", "corporate_actions", "daily_bars", "trading_calendar"]

[job.daily.groups.capital]
steps = ["margin_trading", "compact"]

[job.events.groups.disclosures]
steps = ["announcement_index", "compact"]
"""


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _state(payload: dict | None = None) -> dict:
    value = {"coverage_status": "complete", "complete_through": TARGET}
    value.update(payload or {})
    return value


class FakeLake:
    """Minimal LakeService surface used by the pipeline (read-only + materialize)."""

    def __init__(self, root: Path, rows: list[dict], states: dict[str, dict]):
        self.root = root
        self.rows = rows
        self.states = states
        self.materialize_calls = 0
        self.materialize_result = {"manifest_id": "f" * 64,
                                   "snapshot_db": str(root / "var/research_inputs/current/khquant_raw.db")}

    def catalog(self):
        return {"datasets": [dict(row) for row in self.rows], "count": len(self.rows),
                "data_root": str(self.root / "var/lake")}

    def state(self, name):
        return dict(self.states.get(name, {}))

    def materialize_legacy(self, progress=None, check_cancel=None):
        self.materialize_calls += 1
        target = self.root / "var/research_inputs/current"
        target.mkdir(parents=True, exist_ok=True)
        _write_json(target / "khquant_raw.manifest.json",
                    {"manifest_id": self.materialize_result["manifest_id"],
                     "status": "complete",
                     "revision_map": {name: f"rev-{name}" for name in REQUIRED}})
        return dict(self.materialize_result)


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "lake"
    (root / "configs").mkdir(parents=True)
    (root / "configs/cnequity.toml").write_text(CONFIG, encoding="utf-8")
    return root


@pytest.fixture
def patched(monkeypatch):
    """Replace the outside-world lookups so tests never read the real workspace."""
    monkeypatch.setattr(pipeline, "_calendar_sessions",
                        lambda root, lake=None: (list(SESSIONS), "test_calendar", None))
    monkeypatch.setattr(pipeline, "_resolve_release", lambda root: (dict(RELEASE), None))
    monkeypatch.setattr(pipeline, "_dataset_registry",
                        lambda: {name: {"name": name} for name in REGISTERED})
    monkeypatch.setattr(pipeline, "_disabled_datasets",
                        lambda root, registry, required: set())
    monkeypatch.setattr(pipeline, "_manifest_results", lambda root: {})


def build_lake(root: Path, overrides: dict[str, dict] | None = None) -> FakeLake:
    overrides = overrides or {}
    rows, states = [], {}
    for name in REGISTERED:
        state = _state({"revision_id": f"rev-{name}"})
        state.update(overrides.get(name, {}))
        states[name] = state
        _write_json(root / "var/lake/meta/state" / f"{name}.json", state)
        rows.append({"name": name, "revision_id": state.get("revision_id"),
                     "has_data": state.get("coverage_status") == "complete",
                     "watermark": state.get("complete_through"),
                     "coverage_start": "2015-01-05",
                     "coverage_end": state.get("complete_through") or TARGET})
    return FakeLake(root, rows, states)


def set_state(root: Path, lake: FakeLake, name: str, payload: dict) -> None:
    state = {**lake.states[name], **payload}
    lake.states[name] = state
    _write_json(root / "var/lake/meta/state" / f"{name}.json", state)


def replace_state(root: Path, lake: FakeLake, name: str, payload: dict) -> None:
    lake.states[name] = dict(payload)
    _write_json(root / "var/lake/meta/state" / f"{name}.json", payload)


def freeze_current_input(root: Path) -> None:
    _write_json(root / "var/research_inputs/current/khquant_raw.manifest.json",
                {"manifest_id": "0" * 64, "status": "complete",
                 "revision_map": {name: f"rev-{name}" for name in REQUIRED}})


# ---------------------------------------------------------------------------
# S2 coverage conclusion
# ---------------------------------------------------------------------------


def test_required_partial_blocks_scan_and_counts_stay_consistent(root, patched):
    lake = build_lake(root)
    set_state(root, lake, "daily_bars",
              {"coverage_status": "incomplete", "complete_through": "2026-10-21",
               "outstanding_keys": [{"reason": "unresolved_tip"}] * 2370})
    preview = PipelineService(root, lake).preview({"end": TARGET})
    coverage = preview["coverage"]

    assert coverage["verdict"] == "blocked"
    assert preview["can_scan"] is False
    assert preview["can_backtest"] is False
    assert {"dataset": "daily_bars", "reason": "required_coverage_partial"} in coverage["blocking"]

    counts = coverage["counts"]
    assert counts["attempted"] == sum(counts[key] for key in
                                     ("complete", "partial", "unknown", "failed"))
    assert counts["registered"] == (counts["attempted"] + counts["not_enabled"]
                                    + counts["not_scheduled"])
    assert counts == {"registered": 9, "not_enabled": 0, "not_scheduled": 2, "attempted": 7,
                      "complete": 6, "partial": 1, "unknown": 0, "failed": 0}

    daily = next(entry for entry in coverage["datasets"] if entry["name"] == "daily_bars")
    assert daily["tier"] == "A"
    assert daily["coverage_status"] == "partial"  # 'incomplete' normalised, never complete
    assert daily["missing_keys"] == 2370
    assert daily["reason_code"] == "unresolved_tip"
    assert daily["bucket"] == "partial"
    assert daily["complete_through"] == "2026-10-21"

    active = {entry["name"]: entry for entry in coverage["datasets"]}
    assert active["top_holders"]["tier"] == "C"
    assert active["margin_trading"]["tier"] == "B"


def test_required_complete_allows_scan_and_backtest(root, patched):
    lake = build_lake(root)
    preview = PipelineService(root, lake).preview({"end": TARGET})
    coverage = preview["coverage"]
    assert coverage["verdict"] == "complete"
    assert preview["can_scan"] is True
    assert preview["can_backtest"] is True
    assert coverage["blocking"] == []
    assert coverage["required_datasets"] == REQUIRED
    assert coverage["required_source"] in {"release_data_contract",
                                          "fallback_default_contract"}
    assert coverage["calendar_session"] is True
    # B/C tier datasets never veto the A tier.
    assert all(entry["tier"] in {"A", "B", "C"} for entry in coverage["datasets"])


def test_optional_dataset_gaps_do_not_degrade_the_equity_pipeline(root, patched):
    lake = build_lake(root)
    replace_state(root, lake, "margin_trading",
                  {"coverage_status": "unknown", "complete_through": None})
    for row in lake.rows:
        if row["name"] == "margin_trading":
            row["revision_id"] = None
            row["has_data"] = False
    preview = PipelineService(root, lake).preview({"end": TARGET})
    coverage = preview["coverage"]
    assert coverage["verdict"] == "complete"
    assert preview["can_scan"] is True
    assert coverage["blocking"] == []
    assert any(gap["dataset"] == "margin_trading" for gap in coverage["optional_gaps"])


def test_required_datasets_prefer_the_frozen_release_contract():
    from lake_equity.learning import releases as releases_module

    if not callable(getattr(releases_module, "data_contract", None)):
        pytest.skip("the release module exposes no data_contract helper yet")
    names, source = pipeline._required_datasets(
        {"release_id": RELEASE["release_id"], "data_contract": {"required": ["daily_bars"]}})
    assert names == ["daily_bars"]
    assert source == "release_data_contract"


def test_absent_coverage_verdict_infers_recency_but_never_masks_staleness(root, patched):
    lake = build_lake(root)
    for row in lake.rows:  # instruments carries no watermark in the real catalog
        if row["name"] == "instruments":
            row.update(watermark=None, coverage_end=None)
    # instruments reports only a revision and a last-success date (no coverage verdict).
    replace_state(root, lake, "instruments",
                  {"revision_id": "rev-instruments", "last_success_trade_date": TARGET})
    preview = PipelineService(root, lake).preview({"end": TARGET})
    instruments = next(entry for entry in preview["coverage"]["datasets"]
                       if entry["name"] == "instruments")
    assert instruments["bucket"] == "complete"
    assert instruments["coverage_source"] == "inferred"
    assert preview["can_scan"] is True

    # Yesterday's proof keeps it stale, but instruments is optional and cannot veto.
    replace_state(root, lake, "instruments",
                  {"revision_id": "rev-instruments",
                   "revision_at": "2026-10-22T13:33:35+00:00"})
    preview = PipelineService(root, lake).preview({"end": TARGET})
    instruments = next(entry for entry in preview["coverage"]["datasets"]
                       if entry["name"] == "instruments")
    assert instruments["bucket"] == "partial"
    assert preview["coverage"]["verdict"] == "complete"
    assert preview["can_scan"] is True

    # The required tier is what actually blocks: stale daily_bars keeps it partial.
    set_state(root, lake, "daily_bars",
              {"coverage_status": "incomplete", "complete_through": "2026-10-21",
               "outstanding_keys": [{"reason": "unresolved_tip"}]})
    preview = PipelineService(root, lake).preview({"end": TARGET})
    assert preview["coverage"]["verdict"] == "blocked"
    assert preview["can_scan"] is False


def test_refit_now_records_candidate_without_changing_the_run_status(root, patched, monkeypatch):
    lake = build_lake(root)
    freeze_current_input(root)

    class Workflow:
        def scan(self, params, progress=None, check_cancel=None):
            return {"run_id": "scan-" + "3" * 32, "status": "completed",
                    "final_ranking": True, "as_of": TARGET, "read_ref": "c" * 64,
                    "release_id": RELEASE["release_id"],
                    "population": {"total": 10, "scored": 10, "missing": 0, "failed": 0,
                                   "unprocessed": 0},
                    "top": [{"symbol": "600000.SH", "score": 1.0, "rank": 1}]}

        def backtest(self, params, progress=None, check_cancel=None):
            return {"run_id": "backtest-" + "4" * 32, "status": "completed",
                    "mode": params["mode"], "actual_start": params["start"],
                    "actual_end": params["end"], "read_ref": "d" * 64,
                    "release_id": RELEASE["release_id"],
                    "portfolio": {"metrics": {"total_return": 0.1, "fills": 3},
                                  "trades": [{"symbol": "600000.SH"}],
                                  "evaluation_start": params["start"],
                                  "evaluation_end": params["end"]}}

    monkeypatch.setattr(pipeline, "_workflow", lambda root: Workflow())
    monkeypatch.setattr(pipeline, "_call_refit",
                        lambda root, release, target, progress, cancel: (
                            {"model_id": "model-" + "9" * 32,
                             "release_id": "release-" + "8" * 32,
                             "report_run_id": "refit-run"}, None))
    report = PipelineService(root, lake).run(
        {"end": TARGET, "skip_update": True, "refit": "now"})

    assert report["refit"]["action"] == "refit"
    assert report["refit"]["candidate_model_id"] == "model-" + "9" * 32
    assert report["refit"]["candidate_release_id"] == "release-" + "8" * 32
    assert report["status"] == "completed"
    assert report["exit_code"] == 0
    assert report["backtest"]["performance_status"] == "available"


def test_preview_writes_nothing(root, patched):
    lake = build_lake(root)
    before = {path.relative_to(root) for path in root.rglob("*")}
    preview = PipelineService(root, lake).preview({"end": TARGET})
    after = {path.relative_to(root) for path in root.rglob("*")}
    assert before == after
    assert preview["schema"] == "lake_pipeline_preview_v1"
    assert preview["target_date"] == TARGET
    assert [item["id"] for item in preview["plan"]] == [f"S{index}" for index in range(8)]
    assert preview["active_release"]["release_id"] == RELEASE["release_id"]
    assert lake.materialize_calls == 0


# ---------------------------------------------------------------------------
# S0/S1 run behaviour
# ---------------------------------------------------------------------------


def test_dry_run_skips_subprocess_and_materialize(root, patched, monkeypatch):
    lake = build_lake(root)

    def forbidden(*args, **kwargs):
        raise AssertionError("dry_run must not call Popen or the learning workflow")

    monkeypatch.setattr(pipeline.subprocess, "Popen", forbidden)
    monkeypatch.setattr(pipeline, "_workflow", forbidden)
    report = PipelineService(root, lake).run({"end": TARGET, "dry_run": True})

    assert report["status"] == "completed"
    assert report["exit_code"] == 0
    assert report["data_update"]["status"] == "skipped"
    assert report["data_update"]["reason"] == "dry_run"
    assert report["materialize"]["reason"] == "dry_run"
    assert report["scan"]["reason"] == "dry_run"
    assert report["backtest"]["reason"] == "dry_run"
    assert lake.materialize_calls == 0
    assert not list(root.rglob("*.building"))


def test_missing_active_release_blocks_before_any_stage(root, patched, monkeypatch):
    monkeypatch.setattr(pipeline, "_resolve_release",
                        lambda root: (None, "No active research release"))
    lake = build_lake(root)
    report = PipelineService(root, lake).run({"end": TARGET, "dry_run": True})

    assert report["status"] == "blocked"
    assert report["exit_code"] == 4
    assert "no_active_release" in (report["error"] or "")
    stages = {stage["id"]: stage for stage in report["stages"]}
    assert stages["S0"]["status"] == "blocked"
    for stage_id in ("S1", "S2", "S3", "S4", "S5", "S6"):
        assert stages[stage_id]["status"] == "skipped"
    assert lake.materialize_calls == 0


def test_run_data_update_builds_upstream_command_and_reports_exit_code(root, monkeypatch):
    captured: dict = {}

    class FakeProcess:
        pid = 4242
        returncode = 1

        def poll(self):
            return self.returncode

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            return None

    def fake_popen(command, **kwargs):
        captured["command"] = list(command)
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(pipeline.subprocess, "Popen", fake_popen)
    result = run_data_update(root, object(), {"end": TARGET, "packs": "core"})

    command = captured["command"]
    assert command[:5] == [sys.executable, "-m", "cnequity", "run", "daily"]
    assert command[command.index("--config") + 1] == str(root / "configs/cnequity.toml")
    assert command[command.index("--trade-date") + 1] == TARGET
    assert "--core-only" in command
    assert captured["cwd"] == str(root)
    assert result["status"] == "failed"
    assert result["exit_code"] == 1
    assert result["outcomes"] == []
    assert (root / "var/logs/cne-daily.log").is_file()


def test_run_data_update_terminates_child_on_job_style_cancel(root, monkeypatch):
    from lake_equity.jobs import JobCancelled

    events: dict = {}

    class FakeProcess:
        pid = 7
        returncode = -15

        def poll(self):
            return None  # still running until terminated

        def terminate(self):
            events["terminated"] = True

        def wait(self, timeout=None):
            return -15

        def kill(self):
            events["killed"] = True

    monkeypatch.setattr(pipeline.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(pipeline.time, "sleep", lambda seconds: None)

    def cancelled():
        raise JobCancelled("任务已取消")

    with pytest.raises(JobCancelled):
        run_data_update(root, object(), {"end": TARGET}, cancelled=cancelled)
    assert events.get("terminated") is True


def test_run_data_update_without_config_refuses_before_spawning(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("must not spawn without configs/cnequity.toml")

    monkeypatch.setattr(pipeline.subprocess, "Popen", forbidden)
    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(ValueError, match="configs/cnequity.toml"):
        run_data_update(bare, object(), {})


# ---------------------------------------------------------------------------
# S5/S6 gates
# ---------------------------------------------------------------------------


def test_scan_without_final_ranking_blocks_and_writes_no_candidates(root, patched, monkeypatch):
    lake = build_lake(root)
    freeze_current_input(root)

    class Workflow:
        def scan(self, params, progress=None, check_cancel=None):
            return {"run_id": "scan-" + "1" * 32, "status": "completed",
                    "final_ranking": False, "as_of": TARGET, "read_ref": "a" * 64,
                    "population": {"total": 10, "scored": 0, "missing": 10, "failed": 0,
                                   "unprocessed": 0},
                    "top": [{"symbol": "600000.SH", "score": 1.0, "rank": 1}]}

        def backtest(self, params, progress=None, check_cancel=None):
            raise AssertionError("backtest must not run once scanning is blocked")

    monkeypatch.setattr(pipeline, "_workflow", lambda root: Workflow())
    report = PipelineService(root, lake).run({"end": TARGET, "skip_update": True})

    assert report["status"] == "blocked"
    assert report["exit_code"] == 4
    assert report["scan"]["final_ranking"] is False
    assert report["scan"]["candidates_written"] is False
    assert report["scan"]["top"] == []
    assert report["backtest"]["status"] == "skipped"
    assert lake.materialize_calls == 0  # frozen revision map matched, so S3 skipped
    assert report["materialize"]["reason"] == "already_frozen"


def test_backtest_unavailable_is_degraded_not_success(root, patched, monkeypatch):
    lake = build_lake(root)
    freeze_current_input(root)

    class Workflow:
        def scan(self, params, progress=None, check_cancel=None):
            return {"run_id": "scan-" + "2" * 32, "status": "completed",
                    "final_ranking": True, "as_of": TARGET, "read_ref": "b" * 64,
                    "release_id": RELEASE["release_id"],
                    "population": {"total": 10, "scored": 10, "missing": 0, "failed": 0,
                                   "unprocessed": 0},
                    "top": [{"symbol": "600000.SH", "score": 1.0, "rank": 1}]}

        def backtest(self, params, progress=None, check_cancel=None):
            raise ValueError("No published verified trading sessions in the requested interval")

    monkeypatch.setattr(pipeline, "_workflow", lambda root: Workflow())
    report = PipelineService(root, lake).run({"end": TARGET, "skip_update": True})

    assert report["status"] == "completed_degraded"
    assert report["exit_code"] == 3
    assert report["scan"]["candidates_written"] is True
    assert report["scan"]["top"][0]["symbol"] == "600000.SH"
    assert report["backtest"]["performance_status"] == "unavailable"
    assert "insufficient_data" == report["backtest"]["reason"]


# ---------------------------------------------------------------------------
# S7 artifacts
# ---------------------------------------------------------------------------


def test_artifacts_are_atomic_and_readable(root, patched):
    lake = build_lake(root)
    service = PipelineService(root, lake)
    report = service.run({"end": TARGET, "dry_run": True})

    folder = root / "var/pipeline" / report["pipeline_id"]
    assert {path.name for path in folder.iterdir()} == {"pipeline.json", "coverage.json",
                                                        "report.json"}
    saved = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    state = json.loads((folder / "pipeline.json").read_text(encoding="utf-8"))
    coverage = json.loads((folder / "coverage.json").read_text(encoding="utf-8"))

    assert saved["schema"] == "lake_pipeline_v1"
    assert saved["pipeline_id"] == report["pipeline_id"]
    assert saved["exit_code"] == report["exit_code"] == 0
    assert saved["target_date"] == TARGET
    assert [stage["id"] for stage in saved["stages"]] == [f"S{index}" for index in range(8)]
    assert saved["coverage"]["schema"] == "lake_coverage_v1"
    assert state["schema"] == "lake_pipeline_state_v1"
    assert state["pipeline_id"] == report["pipeline_id"]
    assert coverage["pipeline_id"] == report["pipeline_id"]

    assert service.latest()["pipeline_id"] == report["pipeline_id"]
    assert service.read(report["pipeline_id"])["pipeline_id"] == report["pipeline_id"]
    with pytest.raises(KeyError):
        service.read("pipe-" + "0" * 16)
    with pytest.raises(KeyError):
        service.read("../../etc/passwd")


def test_latest_and_read_return_none_before_first_run(root, patched):
    lake = build_lake(root)
    service = PipelineService(root, lake)
    assert service.latest() is None
    with pytest.raises(KeyError):
        service.read("pipe-" + "1" * 16)
