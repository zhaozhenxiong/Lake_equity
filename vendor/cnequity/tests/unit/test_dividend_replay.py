"""Repair replay must preserve provenance, fail closed and isolate event conflicts."""

from datetime import date

import pytest

from cnequity.adapters.baostock.corporate_actions import _FIELDS, fetch_corporate_actions_baostock
from cnequity.adapters.baostock.dividend_replay import decode_wire
from cnequity.config import Config
from cnequity.steps.http_common import verify_raw_archive
from cnequity.storage.raw_archive import RawArchiveError, RawPayloadArchive


def row(day="2021-06-25", cash="0.013", plan="10派0.13元"):
    values = {
        "code": "sz.000560",
        "dividOperateDate": day,
        "dividPayDate": day,
        "dividCashPsBeforeTax": cash,
        "dividCashStock": plan,
    }
    return [values.get(f, "") for f in _FIELDS]


def wire(rows, year=2021):
    import json

    return "\x01".join(
        [
            "00.9.10",
            "14",
            "0",
            "success",
            "query_dividend_data",
            "anonymous",
            "1",
            "2000",
            json.dumps({"record": rows}),
            "sz.000560",
            str(year),
            "operate",
            ",".join(_FIELDS),
            "checksum",
        ]
    ).encode()


def setup_cache(tmp_path, rows):
    cfg = Config(data_root=tmp_path / "lake", sources={"baostock": True}, raw_archive_enabled=True)
    archive = RawPayloadArchive(cfg.meta_root, enabled=True, datasets=["corporate_actions"])
    record = archive.archive(
        "corporate_actions",
        wire(rows),
        source="baostock",
        request_params={"symbol": "sz.000560", "year": 2021, "year_type": "operate"},
        http_metadata={"wire_exact": True},
        run_id="original",
        payload_format="bytes",
    )
    return cfg, record


def replay(cfg, monkeypatch):
    import cnequity.adapters.baostock.corporate_actions as module

    def forbidden(*args, **kwargs):
        pytest.fail("A complete cache hit must not open a network session")

    monkeypatch.setattr(module, "fetch_per_symbol", forbidden)
    issues, metrics = [], {}
    df, failed = fetch_corporate_actions_baostock(
        ["000560.SZ"],
        date(2021, 1, 1),
        date(2021, 12, 31),
        config=cfg,
        repair_years={"000560.SZ": {2021}},
        diagnostics=issues,
        metrics=metrics,
        run_id="replay",
        request_scope="test-payment",
    )
    return df, failed, issues, metrics


def test_replay_is_network_free_and_has_current_receipt(tmp_path, monkeypatch):
    cfg, _ = setup_cache(tmp_path, [row(cash=""), row()])
    df, failed, issues, metrics = replay(cfg, monkeypatch)
    assert df.height == 1 and not failed and not issues
    assert metrics == {"network_requests": 0, "replayed_requests": 1}
    assert verify_raw_archive(
        cfg, "corporate_actions", "replay", source="baostock", request_scope="test-payment"
    )


def test_true_conflict_blocks_only_its_event(tmp_path, monkeypatch):
    cfg, _ = setup_cache(tmp_path, [row(), row(day="2021-07-01", cash="1.4", plan="10派4元")])
    df, failed, issues, metrics = replay(cfg, monkeypatch)
    assert df["ex_date"].to_list() == [date(2021, 6, 25)]
    assert not failed and issues[0]["reason"] == "source_amount_plan_conflict"
    assert issues[0]["ex_date"] == "2021-07-01"
    assert metrics["network_requests"] == 0


def test_duplicate_conflicting_pay_dates_never_last_wins(tmp_path, monkeypatch):
    other = row()
    other[_FIELDS.index("dividPayDate")] = "2021-06-28"
    cfg, _ = setup_cache(tmp_path, [row(), other])
    df, failed, issues, _ = replay(cfg, monkeypatch)
    assert df.is_empty() and not failed
    assert issues[0]["reason"] == "source_duplicate_conflict"


def test_corrupt_cache_fails_closed(tmp_path, monkeypatch):
    cfg, record = setup_cache(tmp_path, [row()])
    (cfg.meta_root / record.payload_path).write_bytes(b"corrupt")
    with pytest.raises(RawArchiveError):
        replay(cfg, monkeypatch)


def test_wire_rejects_wrong_request_or_partial_page():
    raw = wire([row()])
    for symbol, year in [("sz.000561", 2021), ("sz.000560", 2020)]:
        with pytest.raises(ValueError):
            decode_wire(raw, symbol, year)
    with pytest.raises(ValueError):
        decode_wire(raw.replace(b"\x011\x012000\x01", b"\x012\x012000\x01"), "sz.000560", 2021)


def test_network_miss_requests_only_missing_year_and_isolates_conflict():
    class Result:
        error_code = "0"
        fields = list(_FIELDS)

        def __init__(self, rows=()):
            self.rows = iter(rows)

        def next(self):
            self.current = next(self.rows, None)
            return self.current is not None

        def get_row_data(self):
            return self.current

    class Vendor:
        def __init__(self):
            self.calls = []

        def login(self):
            return Result()

        def logout(self):
            pass

        def query_dividend_data(self, code, year, **kwargs):
            self.calls.append(year)
            return Result([row(), row(day="2021-07-01", cash="1.4", plan="10派4元")])

    vendor = Vendor()
    issues, metrics = [], {}
    df, failed = fetch_corporate_actions_baostock(
        ["000560.SZ"],
        date(2016, 1, 1),
        date(2024, 12, 31),
        bs=vendor,
        sleep=lambda _: None,
        repair_years={"000560.SZ": {2021}},
        diagnostics=issues,
        metrics=metrics,
    )
    assert vendor.calls == [2021] and df.height == 1 and not failed
    assert len(issues) == 1 and metrics["network_requests"] == 1
