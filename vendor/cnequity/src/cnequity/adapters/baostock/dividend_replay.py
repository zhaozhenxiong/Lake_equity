"""Strict replay of complete, successful archived single-page dividend responses."""

from __future__ import annotations

import json
from datetime import datetime

from cnequity.storage.raw_archive import RawArchiveError


def decode_wire(raw: bytes, symbol: str, year: int):
    parts = raw.decode("utf-8").split("\x01")
    if (
        len(parts) != 14
        or parts[3:5] != ["success", "query_dividend_data"]
        or parts[6:8] != ["1", "2000"]
        or parts[9:12] != [symbol, str(year), "operate"]
    ):
        raise ValueError("Not a complete supported dividend response")
    fields = [x.strip() for x in parts[12].split(",")]
    required = {
        "code",
        "dividOperateDate",
        "dividPayDate",
        "dividCashPsBeforeTax",
        "dividStocksPs",
        "dividReserveToStockPs",
        "dividCashStock",
    }
    rows = json.loads(parts[8])["record"]
    if (
        len(set(fields)) != len(fields)
        or not required.issubset(fields)
        or not isinstance(rows, list)
        or len(rows) >= 2000
    ):
        raise ValueError("Incomplete dividend schema/page")
    for row in rows:
        if (
            not isinstance(row, list)
            or len(row) != len(fields)
            or any(not isinstance(x, str) for x in row)
            or row[fields.index("code")] != symbol
        ):
            raise ValueError("Dividend response identity/shape mismatch")
    return fields, rows


class DividendReplay:
    def __init__(self, archive, requested):
        self.archive = archive
        self.index = {}
        root = archive.meta_root / "raw/corporate_actions/source=baostock"
        for path in root.rglob("*.json"):
            record = json.loads(path.read_text())
            params = record.get("request_params", {})
            key = (params.get("symbol"), params.get("year"))
            if (
                key not in requested
                or params.get("year_type") != "operate"
                or record.get("dataset") != "corporate_actions"
                or record.get("source") != "baostock"
                or record.get("http_metadata", {}).get("replayed_from")
            ):
                continue
            if record.get("http_metadata", {}).get("wire_exact") is not True:
                continue
            if not record.get("payload_sha256"):
                raise RawArchiveError("Replay requires a payload digest")
            if key not in self.index or record["captured_at"] > self.index[key]["captured_at"]:
                self.index[key] = record

    def get(self, symbol, year, *, run_id, scope):
        record = self.index.get((symbol, year))
        if record is None:
            return None
        # Native reader checks hashes, sizes and paths, never trust cached bytes.
        raw = self.archive.read(record)
        try:
            fields, rows = decode_wire(raw, symbol, year)
        except (ValueError, KeyError, TypeError):
            return None  # unsupported wire is fetched afresh, not guessed
        self.archive.archive(
            "corporate_actions",
            raw,
            source="baostock",
            request_params=record["request_params"],
            run_id=run_id,
            captured_at=datetime.fromisoformat(record["captured_at"]),
            payload_format="bytes",
            request_scope=scope,
            observation_id=f"{run_id}:replay:{symbol}:{year}:{record['payload_sha256']}",
            http_metadata={
                "wire_exact": True,
                "protocol": "baostock",
                "replayed_from": record["metadata_path"],
                "original_run_id": record.get("run_id"),
            },
        )
        return fields, rows
