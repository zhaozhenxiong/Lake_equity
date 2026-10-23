from __future__ import annotations

import gzip
import hashlib
import json
from types import SimpleNamespace

import pytest

from lake_equity.market.taxonomy import recover_taxonomy


def archive(tmp_path, rows, run_id="run-one"):
    payload = json.dumps({"result": {"data": rows}}).encode()
    sha = hashlib.sha256(payload).hexdigest()
    root = tmp_path / "raw/sector_members/source=eastmoney/captured_date=2026-10-22"
    root.mkdir(parents=True, exist_ok=True)
    content = gzip.compress(payload)
    path = root / f"{sha}.gz"
    path.write_bytes(content)
    sidecar = root / f"{sha}.json"
    record = {"dataset": "sector_members", "source": "eastmoney", "run_id": run_id,
        "request_scope": "snapshot:2026-10-22",
        "request_params": {"report": "RPT_BOARD_CONSTITUENT"}, "response_status": 200,
        "http_metadata": {"wire_exact": True}, "payload_path": path.relative_to(tmp_path).as_posix(),
        "metadata_path": sidecar.relative_to(tmp_path).as_posix(), "payload_sha256": sha,
        "payload_bytes": len(payload), "compressed_bytes": len(content), "compression": "gzip"}
    sidecar.write_text(json.dumps(record), encoding="utf-8")
    return path


def inputs(tmp_path):
    store = SimpleNamespace(config=SimpleNamespace(meta_root=tmp_path))
    snapshot = {"datasets": {"sector_members": {"run_id": "run-one"}}}
    members = [{"sector_code": "A", "symbol": "000001.SZ", "source": "eastmoney", "as_of_date": "2026-10-22"}]
    return store, snapshot, members


@pytest.mark.parametrize(("raw_type", "kind"), [("2", "industry"), ("3", "concept"), ("4", "mixed")])
def test_exact_run_archive_recovers_type_and_records_sha(tmp_path, raw_type, kind):
    archive(tmp_path, [{"SECURITY_CODE": "000001", "BOARD_CODE": "A", "BOARD_NAME": "Ignored", "BOARD_TYPE_NEW": raw_type}])
    result = recover_taxonomy(*inputs(tmp_path))
    assert result["boards"]["A"] == {"kind": kind, "verified": True, "raw_types": [raw_type]}
    assert result["status"] == "same_revision_run_archive"
    assert len(result["evidence"][0]["payload_sha256"]) == 64


def test_other_run_missing_member_and_conflicting_types_never_guessed(tmp_path):
    archive(tmp_path, [{"SECURITY_CODE": "000001", "BOARD_CODE": "A", "BOARD_TYPE_NEW": "2"}], run_id="other-run")
    assert recover_taxonomy(*inputs(tmp_path))["boards"]["A"]["kind"] == "mixed"
    archive(tmp_path, [{"SECURITY_CODE": "000002", "BOARD_CODE": "A", "BOARD_TYPE_NEW": "2"}])
    assert recover_taxonomy(*inputs(tmp_path))["boards"]["A"]["verified"] is False
    archive(tmp_path, [{"SECURITY_CODE": "000001", "BOARD_CODE": "A", "BOARD_TYPE_NEW": "2"},
        {"SECURITY_CODE": "000001", "BOARD_CODE": "A", "BOARD_TYPE_NEW": "3"}])
    assert recover_taxonomy(*inputs(tmp_path))["boards"]["A"]["kind"] == "mixed"


def test_payload_corruption_is_not_silent_mixed_fallback(tmp_path):
    path = archive(tmp_path, [{"SECURITY_CODE": "000001", "BOARD_CODE": "A", "BOARD_TYPE_NEW": "2"}])
    path.write_bytes(gzip.compress(b"{}"))
    with pytest.raises(Exception):
        recover_taxonomy(*inputs(tmp_path))


def test_other_snapshot_scope_cannot_classify_current_members(tmp_path):
    archive(tmp_path, [{"SECURITY_CODE": "000001", "BOARD_CODE": "A", "BOARD_TYPE_NEW": "2"}])
    store, snapshot, members = inputs(tmp_path)
    members[0]["as_of_date"] = "2026-10-21"
    assert recover_taxonomy(store, snapshot, members)["boards"]["A"]["verified"] is False
