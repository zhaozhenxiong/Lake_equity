"""Lifecycle checks use only tiny, isolated lakes and local evidence files."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import polars as pl
import pytest
from click.testing import CliRunner

from cnequity.cli.main import cli
from cnequity.config import Config
from cnequity.storage.lifecycle import (
    LifecycleError,
    LifecycleStore,
    reference_fingerprint,
)
from cnequity.storage.revisions import (
    RevisionConsistencyError,
    RevisionStore,
    prune_revision_generations,
)


@pytest.fixture
def lake(tmp_path):
    cfg = Config(data_root=tmp_path / "lake")
    writer = RevisionStore(cfg.meta_root, cfg.curated_root)
    source = cfg.curated_root / "daily_bars/part.parquet"
    source.parent.mkdir(parents=True)
    for i in range(7):
        pl.DataFrame({"trade_date": [date(2026, 1, 1)], "close": [float(i)]}).write_parquet(source)
        writer.commit(
            "daily_bars",
            run_id=f"run-{i}",
            changed_files=[source],
            schema_version=1,
            contract_fingerprint="test",
        )
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "report.json").write_text("{}")
    store = LifecycleStore(cfg.meta_root)
    roots = [{"path": str(evidence), "exclude": []}]
    store.import_manifest(
        {
            "schema_version": 1,
            "meta_root": str(store.meta),
            "reference_roots": roots,
            "reference_fingerprint": reference_fingerprint(roots),
            "holds": {},
        }
    )
    return cfg, store, writer, evidence


def test_mark_preserves_bytes_and_hold_cancels_pending(lake):
    cfg, store, writer, _ = lake
    pointer = writer.current_pointer("daily_bars")
    plan = store.plan(keep=2)
    before = {p: p.read_bytes() for p in cfg.meta_root.rglob("*.parquet")}
    result = store.mark(plan["plan_id"])
    assert result["marked"] == 5
    assert result["logical_bytes_deleted"] == 0
    assert {p: p.read_bytes() for p in before} == before
    assert writer.current_pointer("daily_bars") == pointer
    oid = plan["candidate_ids"][0]
    pending = store.registry()["pending"][oid]
    assert datetime.fromisoformat(pending["not_before"]) - datetime.fromisoformat(
        pending["marked_at"]
    ) == timedelta(days=7)
    store.hold(oid, "Replay input")
    assert oid not in store.registry()["pending"]
    assert oid not in store.plan(keep=2)["candidate_ids"]
    assert writer.current_root("daily_bars", revision=oid.rsplit("/", 1)[-1]).exists()


def test_remarking_does_not_restart_unchanged_observation(lake):
    _, store, _, _ = lake
    store.mark(store.plan()["plan_id"])
    before = store.registry()["pending"]
    store.mark(store.plan()["plan_id"])
    assert store.registry()["pending"] == before


@pytest.mark.parametrize(
    "change", ["hold", "pointer", "content", "new_reference", "missing_reference"]
)
def test_changes_invalidate_saved_plan(lake, change):
    cfg, store, writer, evidence = lake
    plan = store.plan(keep=2)
    oid = plan["candidate_ids"][0]
    if change == "hold":
        store.hold(oid, "New dependency")
    elif change == "pointer":
        source = cfg.curated_root / "daily_bars/part.parquet"
        pl.DataFrame({"close": [99.0]}).write_parquet(source)
        writer.commit(
            "daily_bars",
            run_id="new",
            changed_files=[source],
            schema_version=1,
            contract_fingerprint="test",
        )
    elif change == "content":
        rid = oid.rsplit("/", 1)[-1]
        path = cfg.meta_root / "revisions/data/daily_bars" / rid / "part.parquet"
        path.write_bytes(b"changed")
    elif change == "new_reference":
        (evidence / "new.json").write_text("{}")
    else:
        (evidence / "report.json").unlink()
    with pytest.raises(LifecycleError):
        store.mark(plan["plan_id"])
    assert not store.registry()["pending"]


def test_missing_evidence_root_blocks_and_import_never_releases(lake):
    _, store, _, evidence = lake
    oid = store.plan()["candidate_ids"][0]
    store.hold(oid, "Explicit hold")
    registry = store.registry()
    store.import_manifest({**registry, "holds": {}})
    assert oid in store.registry()["holds"]
    (evidence / "report.json").unlink()
    evidence.rmdir()
    with pytest.raises(LifecycleError, match="unavailable"):
        store.plan()


def test_corrupt_registry_or_pointer_fails_closed(lake):
    cfg, store, _, _ = lake
    store.registry_path.write_text("[]")
    with pytest.raises(LifecycleError):
        prune_revision_generations(cfg.meta_root)
    store.registry_path.unlink()
    (cfg.meta_root / "revisions/daily_bars/current.json").write_text("{}")
    with pytest.raises(RevisionConsistencyError):
        store.inspect()


def test_orphan_generation_and_legacy_are_blocked(lake):
    cfg, store, _, _ = lake
    orphan = cfg.meta_root / "revisions/data/daily_bars/orphan"
    orphan.mkdir()
    (orphan / "unique.txt").write_text("only copy")
    report = store.inspect()
    unknown = [o for o in report["objects"] if o["revision_id"] in ("legacy", "orphan")]
    assert all("unreceipted_generation" in o["blocked_reasons"] for o in unknown)
    assert not any(oid.endswith("/orphan") for oid in store.plan()["candidate_ids"])


def test_symlinks_rejected(lake, tmp_path):
    _, store, _, evidence = lake
    target = tmp_path / "target.json"
    target.write_text("{}")
    try:
        (evidence / "linked.json").symlink_to(target)
    except OSError:
        pytest.skip("Symlinks unavailable")
    with pytest.raises(LifecycleError, match="link"):
        store.plan()


def test_plan_tampering_and_cross_lake_registry_rejected(lake):
    _, store, _, _ = lake
    plan = store.plan()
    path = store.root / "plans" / f"{plan['plan_id']}.json"
    value = json.loads(path.read_text())
    value["keep"] = 1
    path.write_text(json.dumps(value))
    with pytest.raises(LifecycleError, match="modified"):
        store.mark(plan["plan_id"])
    registry = store.registry()
    registry["meta_root"] = "/different/lake"
    store.registry_path.write_text(json.dumps(registry))
    with pytest.raises(LifecycleError, match="identity"):
        store.inspect()


def test_legacy_prune_requires_import_and_never_deletes(lake):
    cfg, store, _, _ = lake
    store.registry_path.unlink()
    assert prune_revision_generations(cfg.meta_root, dry_run=True)
    with pytest.raises(LifecycleError, match="Import"):
        prune_revision_generations(cfg.meta_root)
    assert len(list((cfg.meta_root / "revisions/data/daily_bars").iterdir())) == 8


def test_dry_run_does_not_create_an_empty_lake(tmp_path):
    meta = tmp_path / "absent/meta"
    assert prune_revision_generations(meta, dry_run=True) == []
    assert not meta.exists()


def test_cli_storage_purge_requires_explicit_phase_and_maintenance():
    result = CliRunner().invoke(cli, ["storage", "apply", "--help"])
    assert result.exit_code == 0
    assert "--phase [mark|purge]" in result.output
    assert "--maintenance-window" in result.output
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "storage" in result.output
    assert "其它" not in result.output


def test_cli_plan_mark_and_stale_error(lake, monkeypatch):
    cfg, store, _, evidence = lake
    monkeypatch.setattr("cnequity.cli.storage_cmds._cfg", lambda _: cfg)
    runner = CliRunner()
    result = runner.invoke(cli, ["storage", "plan", "--keep", "2"])
    assert result.exit_code == 0, result.output
    plan_id = json.loads(result.stdout)["plan_id"]
    result = runner.invoke(cli, ["storage", "apply", plan_id])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["logical_bytes_deleted"] == 0
    result = runner.invoke(cli, ["storage", "plan"])
    plan_id = json.loads(result.stdout)["plan_id"]
    (evidence / "new.json").write_text("{}")
    result = runner.invoke(cli, ["storage", "apply", plan_id])
    assert result.exit_code == 1
    assert "Reference inventory changed" in result.output
    assert store.registry()["pending"]


def test_clean_without_import_is_preview_only(lake, monkeypatch):
    cfg, store, _, _ = lake
    store.registry_path.unlink()
    monkeypatch.setattr("cnequity.cli.maintain_cmds._cfg", lambda _: cfg)
    result = CliRunner().invoke(cli, ["run", "clean"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["dry_run"] is True
    assert report["bytes_freed"] == 0
    assert not store.registry_path.exists()
