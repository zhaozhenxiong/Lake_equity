import hashlib
import json
import shutil
from datetime import date

import polars as pl
import pytest

from cnequity.domain.contracts import contract_fingerprint
from cnequity.domain.datasets import DATASETS
from cnequity.storage.revisions import RevisionStore
from lake_equity.market.store import MarketStore, Unavailable


def publish(store, dataset, frame, run_id="synthetic-market-test"):
    spec = DATASETS[dataset]
    layer = store.config.derived_root if spec.layer == "derived" else store.config.curated_root
    path = layer / dataset / "fixture.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(path)
    revisions = RevisionStore(store.config.meta_root, store.config.curated_root, store.config.derived_root)
    return revisions.commit(dataset, run_id=run_id, changed_files=[path], schema_version=spec.schema_version, contract_fingerprint=contract_fingerprint(dataset))


def fixture_bars(close=10.0):
    return pl.DataFrame({"sector_code": ["881001"], "trade_date": [date(2026, 10, 22)], "close": [close], "source": ["ths"]})


def rewrite_manifest(store, snapshot):
    identity = {key: value for key, value in snapshot.items() if key != "read_ref"}
    snapshot["read_ref"] = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    (store.reads_root / f"{snapshot['read_ref']}.json").write_text(json.dumps(snapshot), encoding="utf-8")


def test_empty_capture_marks_all_missing_without_creating_lake(tmp_path):
    store = MarketStore(tmp_path)
    snapshot = store.capture()
    assert len(snapshot["datasets"]) == 55
    assert not any(item["available"] for item in snapshot["datasets"].values())
    assert snapshot["revision_map"] == {}
    assert snapshot["atomic_across_datasets"] is False
    assert not store.data_root.exists()
    assert store.capture(snapshot["read_ref"]) == snapshot
    with pytest.raises(Unavailable):
        store.scan("sector_bars", snapshot)


def test_capture_pins_old_generation_after_new_publish_and_pointer_removal(tmp_path):
    store = MarketStore(tmp_path)
    first = publish(store, "sector_bars", fixture_bars())
    snapshot = store.capture()
    item = snapshot["datasets"]["sector_bars"]
    assert item["rows"] == 1
    assert item["coverage_start"] == item["coverage_end"] == "2026-10-22"
    assert item["units"]["amount"] == "source_native"
    assert item["receipt"]["revision_id"] == first.revision_id
    second = publish(store, "sector_bars", fixture_bars(20.0), "another-synthetic-run")
    assert second.revision_id != first.revision_id
    store.revisions.pointer_path("sector_bars").unlink()
    shutil.rmtree(store.config.curated_root / "sector_bars")
    assert store.capture(snapshot["read_ref"]) == snapshot
    assert store.frame("sector_bars", snapshot)["close"].to_list() == [10.0]


def test_revision_zero_is_not_promoted_to_published(tmp_path):
    store = MarketStore(tmp_path)
    path = store.config.curated_root / "sector_bars" / "fixture.parquet"
    path.parent.mkdir(parents=True)
    fixture_bars().write_parquet(path)
    revisions = RevisionStore(store.config.meta_root, store.config.curated_root, store.config.derived_root)
    revisions.ensure_current("sector_bars")
    snapshot = store.capture()
    assert snapshot["datasets"]["sector_bars"]["available"] is False
    assert "sector_bars" not in snapshot["revision_map"]


def test_read_snapshot_checks_manifest_without_current_or_generation_access(tmp_path, monkeypatch):
    store = MarketStore(tmp_path)
    publish(store, "sector_bars", fixture_bars())
    snapshot = store.capture()

    def forbidden(*args, **kwargs):
        raise AssertionError("Manifest restoration must not inspect current or generations")

    monkeypatch.setattr(store.revisions, "current_pointer", forbidden)
    monkeypatch.setattr(store, "_generation", forbidden)
    assert store.read_snapshot(snapshot["read_ref"]) == snapshot


def test_read_snapshot_leaves_unrelated_generation_validation_to_scan(tmp_path, monkeypatch):
    store = MarketStore(tmp_path)
    publish(store, "sector_bars", fixture_bars())
    publish(store, "market_breadth", pl.DataFrame({"trade_date": [date(2026, 10, 22)], "metric_id": ["advance_count"], "value": [1.0]}))
    snapshot = store.capture()
    shutil.rmtree(snapshot["datasets"]["market_breadth"]["path"])
    inspected = []
    validate = store._generation

    def selected_only(dataset, item):
        inspected.append(dataset)
        return validate(dataset, item)

    monkeypatch.setattr(store, "_generation", selected_only)
    restored = store.read_snapshot(snapshot["read_ref"])
    assert inspected == []
    assert store.scan("sector_bars", restored).collect()["close"].to_list() == [10.0]
    assert inspected == ["sector_bars"]
    with pytest.raises(Unavailable, match="missing"):
        store.scan("market_breadth", restored)


def test_read_snapshot_still_rejects_manifest_identity_tampering(tmp_path):
    store = MarketStore(tmp_path)
    snapshot = store.capture()
    path = store.reads_root / f"{snapshot['read_ref']}.json"
    changed = json.loads(path.read_text())
    changed["atomic_across_datasets"] = True
    path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="identity mismatch"):
        store.read_snapshot(snapshot["read_ref"])


@pytest.mark.parametrize("read_ref", ["../current", "x" * 64, "A" * 64, "", None])
def test_invalid_read_references_are_rejected(tmp_path, read_ref):
    store = MarketStore(tmp_path)
    with pytest.raises(ValueError):
        store._manifest(read_ref)


def test_manifest_and_caller_tampering_fail_closed(tmp_path):
    store = MarketStore(tmp_path)
    publish(store, "sector_bars", fixture_bars())
    snapshot = store.capture()
    changed = json.loads(json.dumps(snapshot))
    changed["datasets"]["sector_bars"]["rows"] = 100
    with pytest.raises(ValueError, match="differs"):
        store.scan("sector_bars", changed)
    path = store.reads_root / f"{snapshot['read_ref']}.json"
    path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="identity mismatch"):
        store.capture(snapshot["read_ref"])


def test_rehashed_manifest_cannot_escape_generation_or_receipt(tmp_path):
    store = MarketStore(tmp_path)
    publish(store, "sector_bars", fixture_bars())
    snapshot = store.capture()
    snapshot["datasets"]["sector_bars"]["path"] = str(tmp_path / "outside")
    rewrite_manifest(store, snapshot)
    with pytest.raises(Unavailable, match="mismatched"):
        store.capture(snapshot["read_ref"])


def test_missing_generation_never_falls_back_to_latest(tmp_path):
    store = MarketStore(tmp_path)
    publish(store, "sector_bars", fixture_bars())
    snapshot = store.capture()
    publish(store, "sector_bars", fixture_bars(99.0))
    shutil.rmtree(snapshot["datasets"]["sector_bars"]["path"])
    with pytest.raises(Unavailable, match="missing"):
        store.capture(snapshot["read_ref"])


def test_changed_receipt_and_generation_file_are_rejected(tmp_path):
    store = MarketStore(tmp_path)
    publish(store, "sector_bars", fixture_bars())
    snapshot = store.capture()
    item = snapshot["datasets"]["sector_bars"]
    receipt_path = store.config.meta_root / item["receipt_path"]
    receipt_path.write_bytes(receipt_path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="receipt bytes"):
        store.capture(snapshot["read_ref"])


def test_relative_config_root_is_project_relative_not_cwd(tmp_path):
    config = tmp_path / "configs" / "cnequity.toml"
    config.parent.mkdir()
    config.write_text('[data]\nroot = "var/custom_lake"\n', encoding="utf-8")
    store = MarketStore(tmp_path)
    assert store.data_root == tmp_path / "var" / "custom_lake"
    assert store.config.data_root == store.data_root


def test_config_cannot_read_a_different_project_lake(tmp_path):
    config = tmp_path / "configs" / "cnequity.toml"
    config.parent.mkdir()
    config.write_text('[data]\nroot = "../outside"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="escapes"):
        MarketStore(tmp_path)
