"""Full-market scans chain the latest-day board build; never recompute stock structure.

The linkage switch validates at the API boundary, the chain only fires for
succeeded/partial scans of the latest qualified day with a ready board catalog,
and a linkage failure never changes the scan's own outcome.
"""
import json
import time

import pytest

from my_strategy.storage.czsc_results import ResultStore
from my_strategy.web_dashboard.api import TaskRequest
from my_strategy.web_dashboard.tasks import TaskManager


@pytest.fixture
def manager(tmp_path):
    instance = TaskManager(tmp_path/"metadata/tasks.db", ResultStore(tmp_path/"results.db", tmp_path/"runs"), workers=1)
    yield instance
    instance.close()


def terminal(manager, job_id):
    deadline = time.monotonic()+5
    while time.monotonic() < deadline:
        result = manager.get(job_id)
        if result["status"] not in {"queued", "running", "cancelling"}:
            return result
        time.sleep(.01)
    raise AssertionError("current task did not finish")


def scan_result(day="2026-10-16"):
    return {"run_id": "scan-board-link", "status": "completed", "data_end": day,
            "data_range": {"start": "2026-01-01", "end": day}, "training_started": False}


@pytest.fixture
def board_deps(monkeypatch):
    state = {"latest": "2026-10-16", "boards": [{"board_id": "B1", "kind": "industry"}]}
    import my_strategy.services.board_fluent as fluent
    import my_strategy.storage.board_knowledge as knowledge

    class FakeKnowledge:
        def __init__(self, *args, **kwargs): pass
        def catalog(self, **kwargs): return list(state["boards"])

    monkeypatch.setattr(fluent, "latest_qualified_price_date", lambda *a, **k: state["latest"])
    monkeypatch.setattr(knowledge, "BoardKnowledgeStore", FakeKnowledge)
    return state


@pytest.fixture
def chained(manager, monkeypatch):
    calls = []
    original = manager.submit

    def spy(kind, spec):
        if kind == "board_features":
            calls.append(spec)
            return {"job_id": "board-stub", "kind": kind, "spec": spec, "status": "queued"}
        return original(kind, spec)

    monkeypatch.setattr(manager, "submit", spy)
    return calls


def test_update_rejects_link_boards():
    with pytest.raises(ValueError, match="板块联动"):
        TaskRequest(kind="update", spec={"link_boards": True})


@pytest.mark.parametrize("value", [None, True, False])
def test_scan_accepts_link_boards(value):
    request = TaskRequest(kind="scan", spec={"link_boards": value})
    assert request.spec.link_boards is value


def test_full_market_scan_chains_board_build(manager, board_deps, chained, monkeypatch):
    monkeypatch.setattr(manager, "_run", lambda job_id, kind, spec: scan_result())
    job = manager.submit("scan", {"symbols": [], "end": "2026-10-16"})
    final = terminal(manager, job["job_id"])
    assert final["status"] == "succeeded", final["error"]
    assert chained == [{"mode": "current_reference", "price_date": "2026-10-16", "with_native": True}]
    assert final["linked_board"] == {"job_id": "board-stub", "status": "queued", "price_date": "2026-10-16"}


def test_symbol_scan_does_not_chain_by_default(manager, board_deps, chained, monkeypatch):
    monkeypatch.setattr(manager, "_run", lambda job_id, kind, spec: scan_result())
    job = manager.submit("scan", {"symbols": ["000001.SZ"], "end": "2026-10-16"})
    final = terminal(manager, job["job_id"])
    assert final["status"] == "succeeded", final["error"]
    assert chained == [] and "linked_board" not in final


def test_symbol_scan_chains_when_explicit(manager, board_deps, chained, monkeypatch):
    monkeypatch.setattr(manager, "_run", lambda job_id, kind, spec: scan_result())
    job = manager.submit("scan", {"symbols": ["000001.SZ"], "end": "2026-10-16", "link_boards": True})
    final = terminal(manager, job["job_id"])
    assert final["status"] == "succeeded", final["error"]
    assert len(chained) == 1


def test_opt_out_disables_chain(manager, board_deps, chained, monkeypatch):
    monkeypatch.setattr(manager, "_run", lambda job_id, kind, spec: scan_result())
    job = manager.submit("scan", {"symbols": [], "end": "2026-10-16", "link_boards": False})
    final = terminal(manager, job["job_id"])
    assert final["status"] == "succeeded", final["error"]
    assert chained == [] and "linked_board" not in final


def test_historical_scan_skips_with_reason(manager, board_deps, chained, monkeypatch):
    board_deps["latest"] = "2026-10-19"
    monkeypatch.setattr(manager, "_run", lambda job_id, kind, spec: scan_result(day="2026-10-16"))
    job = manager.submit("scan", {"symbols": [], "end": "2026-10-16"})
    final = terminal(manager, job["job_id"])
    assert final["status"] == "succeeded", final["error"]
    assert chained == []
    assert final["linked_board"]["skipped"] is True
    assert "2026-10-19" in final["linked_board"]["reason"]


def test_empty_catalog_skips_with_reason(manager, board_deps, chained, monkeypatch):
    board_deps["boards"] = []
    monkeypatch.setattr(manager, "_run", lambda job_id, kind, spec: scan_result())
    job = manager.submit("scan", {"symbols": [], "end": "2026-10-16"})
    final = terminal(manager, job["job_id"])
    assert final["status"] == "succeeded", final["error"]
    assert chained == []
    assert final["linked_board"]["skipped"] is True
    assert "板块成员库" in final["linked_board"]["reason"]


def test_chain_failure_never_fails_scan(manager, board_deps, monkeypatch):
    monkeypatch.setattr(manager, "_run", lambda job_id, kind, spec: scan_result())
    original = manager.submit
    calls = []

    def spy(kind, spec):
        if kind == "board_features":
            calls.append(spec)
            raise RuntimeError("synthetic submit failure")
        return original(kind, spec)

    monkeypatch.setattr(manager, "submit", spy)
    job = manager.submit("scan", {"symbols": [], "end": "2026-10-16"})
    final = terminal(manager, job["job_id"])
    assert final["status"] == "succeeded"
    assert calls and final["linked_board"]["skipped"] is True
    assert "synthetic submit failure" in final["linked_board"]["reason"]


def test_failed_scan_never_chains(manager, board_deps, chained, monkeypatch):
    monkeypatch.setattr(manager, "_run", lambda job_id, kind, spec: {**scan_result(), "status": "failed", "error": "x"})
    job = manager.submit("scan", {"symbols": [], "end": "2026-10-16"})
    final = terminal(manager, job["job_id"])
    assert final["status"] == "failed"
    assert chained == [] and "linked_board" not in final


def test_rename_and_linkage_markup_present():
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1]/"web_dashboard/static/index.html").read_text(encoding="utf-8")
    assert '>全市场回测</button>' in html and '>今日候选</button>' not in html
    assert 'id="scan-board-link"' in html and 'id="scan-link-boards"' in html
    assert 'id="scan-board-freshness"' in html
