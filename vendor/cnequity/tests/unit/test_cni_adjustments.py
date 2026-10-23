"""Offline coverage for CNI index adjustment helpers."""

from __future__ import annotations

import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from types import SimpleNamespace

import httpx
import polars as pl
import pytest

from cnequity.adapters.cni import index_constituents_history as cni
from cnequity.config import Config


def test_member_symbol_filters_non_a():
    assert cni._member_symbol("600519") == "600519.SH"
    assert cni._member_symbol("000001") == "000001.SZ"
    # Not an A-share equity code
    assert cni._member_symbol("999999") is None


def test_fetch_cni_rejects_empty_payload_and_parse_error(monkeypatch):
    class Resp:
        content = b"x" * 10  # too short

        def raise_for_status(self):
            return None

    client = SimpleNamespace(get=lambda *a, **k: Resp(), close=lambda: None)
    import pytest

    with pytest.raises(cni.CniAdjustmentPayloadError, match="truncated"):
        cni.fetch_cni_index_adjustments("399001.SZ", client=client)

    class BadResp:
        content = b"x" * 200

        def raise_for_status(self):
            return None

    import pandas as pd

    monkeypatch.setattr(
        pd, "read_excel", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad xlsx"))
    )
    with pytest.raises(cni.CniAdjustmentPayloadError, match="malformed"):
        cni.fetch_cni_index_adjustments(
            "399001.SZ",
            client=SimpleNamespace(get=lambda *a, **k: BadResp(), close=lambda: None),
        )


def test_fetch_cni_unsupported_index_returns_typed_empty_without_fetching():
    def fail(*_args, **_kwargs):
        raise AssertionError("unsupported CNI index must not be fetched")

    empty = cni.fetch_cni_index_adjustments(
        "000300.SH", client=SimpleNamespace(get=fail, close=lambda: None)
    )
    assert empty.is_empty()
    assert "index_symbol" in empty.schema


def test_publisher_missing_history_is_not_a_successful_empty_book():
    for code in ("399001", "399006"):

        class Resp:
            content = f"{code}的样本历史调样文件不存在！".encode()

            def raise_for_status(self):
                return None

        client = SimpleNamespace(get=lambda *a, **k: Resp(), close=lambda: None)
        with pytest.raises(
            cni.CniAdjustmentPayloadError, match="historical coverage is unresolved"
        ):
            cni.fetch_cni_index_adjustments(f"{code}.SZ", client=client)


def test_fetch_cni_parses_xlsx_rows(monkeypatch):
    import pandas as pd

    pdf = pd.DataFrame(
        [
            {
                "开始日期": "2024-01-01",
                "结束日期": "2025-01-01",
                "样本代码": "000001",
                "调整类型": "OLD",
            },
            {
                "开始日期": "2024-01-01",
                "结束日期": "2025-01-01",
                "样本代码": "000001",
                "调整类型": "OLD",
            },
            {
                "开始日期": "2024-01-01",
                "结束日期": "2025-01-01",
                "样本代码": "600519",
                "调整类型": "-",  # removal — skipped
            },
            {
                "开始日期": "2024-06-01",
                "结束日期": "2025-06-01",
                "样本代码": "000002",
                "调整类型": "+",
            },
        ]
    )

    class Resp:
        content = b"x" * 200

        def raise_for_status(self):
            return None

    monkeypatch.setattr(pd, "read_excel", lambda *a, **k: pdf)
    df = cni.fetch_cni_index_adjustments(
        "399001.SZ",
        client=SimpleNamespace(get=lambda *a, **k: Resp(), close=lambda: None),
    )
    assert df.height == 2
    assert set(df["symbol"].to_list()) == {"000001.SZ", "000002.SZ"}


def test_cni_validated_cache_reuses_per_index_and_refreshes(tmp_path, monkeypatch):
    import pandas as pd

    monkeypatch.setenv("CNE_RATE_LIMIT_ROOT", str(tmp_path / "egress"))
    cfg = Config(data_root=tmp_path / "lake", source_intervals={"cni": 0})
    buffer = io.BytesIO()
    pd.DataFrame(
        [
            {
                "开始日期": "2024-01-01",
                "结束日期": "2025-01-01",
                "样本代码": "000001",
                "调整类型": "OLD",
            }
        ]
    ).to_excel(buffer, index=False)
    workbook = buffer.getvalue()
    calls = []
    real_client = httpx.Client

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=workbook)

    def make_client(**_kwargs):
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(cni.httpx, "Client", make_client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        frames = list(
            pool.map(lambda _: cni.fetch_cni_index_adjustments("399001.SZ", config=cfg), range(2))
        )
    assert len(calls) == 1
    assert frames[0].to_dicts() == frames[1].to_dicts()
    assert frames[0].height == 1

    other = cni.fetch_cni_index_adjustments("399006.SZ", config=cfg)
    assert other.height == 1
    assert other["index_symbol"].to_list() == ["399006.SZ"]
    assert len(calls) == 2
    assert "indexcode=399001" in calls[0]
    assert "indexcode=399006" in calls[1]

    from cnequity.domain.rate_limit import _read_json

    assert _read_json(cfg.rate_limit_root / "reuse-cni.json")["hits"] == 1
    cached = cfg.meta_root / "source_cache" / "cni" / "adjustments-399001-"
    path = next(cached.parent.glob(cached.name + "*.json"))
    saved = json.loads(path.read_text())
    saved["sha256"] = "0" * 64
    path.write_text(json.dumps(saved))
    assert cni.fetch_cni_index_adjustments("399001.SZ", config=cfg).height == 1
    assert len(calls) == 3
    saved = json.loads(path.read_text())
    saved["captured_at"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(saved))
    assert cni.fetch_cni_index_adjustments("399001.SZ", config=cfg).height == 1
    assert len(calls) == 4

    def fail_cache(*_args, **_kwargs):
        raise OSError("cache volume full")

    monkeypatch.setattr(cni, "write_json_atomic", fail_cache)
    fresh = Config(data_root=tmp_path / "fresh-lake", source_intervals={"cni": 0})
    assert cni.fetch_cni_index_adjustments("399001.SZ", config=fresh).height == 1
    assert len(calls) == 5


def test_cni_invalid_workbook_does_not_enter_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("CNE_RATE_LIMIT_ROOT", str(tmp_path / "egress"))
    cfg = Config(data_root=tmp_path / "lake", source_intervals={"cni": 0})
    real_client = httpx.Client
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=b"invalid workbook" * 20)

    monkeypatch.setattr(
        cni.httpx,
        "Client",
        lambda **_kwargs: real_client(transport=httpx.MockTransport(handler)),
    )
    for _ in range(2):
        with pytest.raises(cni.CniAdjustmentPayloadError, match="malformed"):
            cni.fetch_cni_index_adjustments("399001.SZ", config=cfg)
    assert len(calls) == 2
    assert not list((cfg.meta_root / "source_cache" / "cni").glob("*.json"))


def test_expand_cni_constituents_as_of():
    adjustments = pl.DataFrame(
        {
            "index_symbol": ["399001.SZ", "399001.SZ"],
            "symbol": ["000001.SZ", "000002.SZ"],
            "start_date": [date(2024, 1, 1), date(2024, 6, 1)],
            "end_date": [date(2025, 1, 1), date(2025, 6, 1)],
            "adjust_type": ["OLD", "+"],
        }
    )
    out = cni.expand_cni_constituents_as_of(adjustments, [date(2024, 3, 1), date(2024, 7, 1)])
    assert out.height == 3  # 000001 on both dates; 000002 only on Jul
    assert cni.expand_cni_constituents_as_of(adjustments, []).is_empty()
    assert cni.expand_cni_constituents_as_of(
        pl.DataFrame(schema=adjustments.schema), [date(2024, 1, 2)]
    ).is_empty()
