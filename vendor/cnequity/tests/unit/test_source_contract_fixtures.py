from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from cnequity.adapters.cninfo.announcements import fetch_announcement_index
from cnequity.adapters.eastmoney.bars import fetch_daily_bars
from cnequity.adapters.tdx_protocol._decode import decoded_quantity_or_none

ROOT = Path(__file__).parents[1] / "fixtures" / "source_contracts"


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _EastmoneyClient:
    def __init__(self, payload):
        self.payload = payload

    def get(self, *args, **kwargs):
        return _Response(self.payload)


class _CninfoClient:
    def __init__(self, pages):
        self.pages = pages

    def post(self, url, data):
        batches = self.pages[data["column"]]
        index = data["pageNum"] - 1
        rows = batches[index] if index < len(batches) else []
        return _Response({"announcements": rows, "hasMore": index + 1 < len(batches)})


def _fixture(name: str):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def test_eastmoney_golden_payload_contract():
    fixture = _fixture("eastmoney_daily_bars.json")
    window = fixture["window"]
    frame = fetch_daily_bars(
        [fixture["symbol"]],
        date.fromisoformat(window["start"]),
        date.fromisoformat(window["end"]),
        client=_EastmoneyClient(fixture["payload"]),
    )
    row = frame.row(0, named=True)
    expected = fixture["expected"]
    assert row["trade_date"].isoformat() == expected.pop("trade_date")
    assert {key: row[key] for key in expected} == expected


def test_cninfo_golden_payload_contract():
    fixture = _fixture("cninfo_announcements.json")
    frame = fetch_announcement_index(
        date.fromisoformat(fixture["trade_date"]), client=_CninfoClient(fixture["pages"])
    )
    expected = fixture["expected"]
    assert frame.height == expected["rows"]
    assert frame.row(0, named=True)["symbol"] == expected["symbol"]
    assert frame.row(0, named=True)["title"] == expected["title"]


def test_tdx_decoder_golden_boundary_contract():
    fixture = _fixture("tdx_decoded_quantities.json")
    assert [decoded_quantity_or_none(item["wire_value"]) for item in fixture["cases"]] == [
        item["expected"] for item in fixture["cases"]
    ]
