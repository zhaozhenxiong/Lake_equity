"""Portable retention evidence survives full snapshots without granting purge."""

import json
from datetime import date

import polars as pl
import pytest

from cnequity.config import Config
from cnequity.storage.atomic import write_json_atomic
from cnequity.storage.lifecycle import LifecycleError, LifecycleStore, digest, reference_fingerprint
from cnequity.storage.lifecycle.snapshot import (
    DEPENDENCIES_PATH,
    export_dependencies,
    read_dependencies,
)
from cnequity.storage.snapshots import SnapshotStore


def imported_store(cfg, evidence):
    evidence.mkdir(exist_ok=True)
    roots = [{"path": str(evidence)}]
    store = LifecycleStore(cfg.meta_root)
    store.import_manifest(
        {
            "schema_version": 1,
            "meta_root": str(store.meta),
            "holds": {
                "revision/daily_bars/previous": [{"reason": "historical verification"}],
            },
            "reference_roots": roots,
            "reference_fingerprint": reference_fingerprint(roots),
        }
    )
    return store


def test_snapshot_restores_protection_without_deletion_authority(tmp_path):
    cfg = Config(data_root=tmp_path / "lake")
    source = cfg.curated_root / "daily_bars/part.parquet"
    source.parent.mkdir(parents=True)
    pl.DataFrame(
        {"symbol": ["600000.SH"], "trade_date": [date(2024, 1, 2)], "close": [10.0]}
    ).write_parquet(source)
    lifecycle = imported_store(cfg, tmp_path / "evidence")
    snapshots = SnapshotStore(cfg)
    manifest = json.loads(snapshots.create("protected", ["daily_bars"]).read_text())
    assert manifest["lifecycle_dependencies"]["requires_rebinding"]
    assert snapshots.verify("protected").passed
    target = snapshots.restore("protected", tmp_path / "restored")
    restored = LifecycleStore(target / "meta")
    dependencies = read_dependencies(restored.meta)
    assert dependencies["protection"]["holds"] == lifecycle.registry()["holds"]
    assert not restored.registry_path.exists()
    with pytest.raises(LifecycleError, match="Import"):
        restored.plan()
    assert export_dependencies(restored) == dependencies
    roots = [{"path": str(tmp_path / "evidence")}]
    restored.import_manifest(
        {
            "schema_version": 1,
            "meta_root": str(restored.meta),
            "holds": {},
            "reference_roots": roots,
            "reference_fingerprint": reference_fingerprint(roots),
        }
    )
    assert restored.registry()["holds"] == lifecycle.registry()["holds"]
    assert restored.registry()["lake_id"] != lifecycle.registry()["lake_id"]
    assert not restored.registry()["pending"]
    with pytest.raises(ValueError, match="full snapshot"):
        snapshots._lake_index(cfg.data_root, ["daily_bars"])


def test_tampered_dependencies_cannot_be_reimported(tmp_path):
    cfg = Config(data_root=tmp_path / "lake")
    store = imported_store(cfg, tmp_path / "evidence")
    value = export_dependencies(store)
    restored = LifecycleStore(tmp_path / "restored/meta")
    value["protection"]["holds"] = {}
    write_json_atomic(restored.meta / DEPENDENCIES_PATH, value)
    with pytest.raises(LifecycleError, match="Invalid"):
        read_dependencies(restored.meta)


def test_digest_valid_but_missing_case_hold_rejected(tmp_path):
    cfg = Config(data_root=tmp_path / "lake")
    store = imported_store(cfg, tmp_path / "evidence")
    value = export_dependencies(store)
    value["protection"]["cases"] = [{"case_id": "test", "roots": ["revision/daily_bars/missing"]}]
    value["protection_digest"] = digest(value["protection"])
    write_json_atomic(store.meta / DEPENDENCIES_PATH, value)
    with pytest.raises(LifecycleError, match="lack protection"):
        read_dependencies(store.meta)
