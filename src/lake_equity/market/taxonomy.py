from __future__ import annotations

import hashlib
import json
from pathlib import Path

from cnequity.adapters.eastmoney.common import exchange_from_datacenter, symbol_from_em
from cnequity.storage.raw_archive import RawPayloadArchive


def recover_taxonomy(store, snapshot: dict, members: list[dict]) -> dict:
    item = snapshot["datasets"]["sector_members"]
    result = {"status": "unverified_mixed", "boards": {}, "evidence": [],
              "rule": "BOARD_TYPE_NEW:2=industry;3=concept;4=mixed;exact_source_code_and_members"}
    run_id = item.get("run_id")
    if not run_id or not hasattr(store, "config"):
        return result
    meta_root = Path(store.config.meta_root)
    archive = RawPayloadArchive(meta_root)
    observed = {}
    scopes = {f"snapshot:{row['as_of_date']}" for row in members if row.get("as_of_date") is not None}
    for path in sorted((meta_root / "raw" / "sector_members").rglob("*.json")):
        raw_meta = path.read_bytes()
        record = json.loads(raw_meta)
        if (record.get("run_id"), record.get("dataset"), record.get("source")) != (run_id, "sector_members", "eastmoney"):
            continue
        if record.get("request_params", {}).get("report") != "RPT_BOARD_CONSTITUENT":
            continue
        if scopes and record.get("request_scope") not in scopes:
            continue
        if record.get("response_status") != 200 or not record.get("http_metadata", {}).get("wire_exact"):
            continue
        if record.get("metadata_path") != path.relative_to(meta_root).as_posix():
            raise ValueError("Sector taxonomy archive path mismatch")
        payload = archive.read(record)
        envelope = json.loads(payload)
        rows = (envelope.get("result") or {}).get("data")
        if not isinstance(rows, list):
            raise ValueError("Sector taxonomy archive has no data rows")
        result["evidence"].append({"metadata_path": record["metadata_path"],
            "metadata_sha256": hashlib.sha256(raw_meta).hexdigest(),
            "payload_sha256": record["payload_sha256"], "run_id": run_id})
        for row in rows:
            exchange = exchange_from_datacenter(row)
            symbol = symbol_from_em(str(row.get("SECURITY_CODE", "")), 1 if exchange == "SH" else 2 if exchange == "BJ" else 0)
            code = str(row.get("BOARD_CODE") or "").strip()
            if symbol and code:
                observed.setdefault((code, symbol), set()).add(str(row.get("BOARD_TYPE_NEW") or ""))
    grouped = {}
    for row in members:
        grouped.setdefault(str(row["sector_code"]), []).append(row)
    for code, population in grouped.items():
        types = set()
        proven = True
        for row in population:
            values = observed.get((code, row["symbol"]), set())
            if row.get("source") != "eastmoney" or len(values) != 1 or not values <= {"2", "3", "4"}:
                proven = False
            types.update(values)
        result["boards"][code] = {"kind": {"2": "industry", "3": "concept"}.get(next(iter(types)), "mixed")
            if proven and len(types) == 1 else "mixed", "verified": proven and len(types) == 1,
            "raw_types": sorted(types)}
    if result["evidence"]:
        result["status"] = "same_revision_run_archive"
    return result
