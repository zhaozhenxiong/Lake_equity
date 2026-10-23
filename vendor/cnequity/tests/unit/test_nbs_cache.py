"""Bounded publisher-page reuse for the NBS PMI cross-check."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from curl_cffi import requests as cr

from cnequity.adapters.nbs import pmi_release as nbs
from cnequity.config import Config
from cnequity.domain.rate_limit import _read_json

INDEX = '<a href="./202607/t20260731_1964253.html">2026年7月中国采购经理指数运行情况</a>'
RELEASE = "<p>制造业采购经理指数（PMI）为49.2%</p>"


class Response:
    status_code = 200
    headers = {}
    encoding = "utf-8"

    def __init__(self, url: str, text: str):
        self.url = url
        self.text = text
        self.content = text.encode("utf-8")

    def raise_for_status(self):
        return None


def test_nbs_validated_pages_merge_concurrent_reads_and_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("CNE_RATE_LIMIT_ROOT", str(tmp_path / "egress"))
    cfg = Config(data_root=tmp_path / "lake", source_intervals={"nbs": 0})
    calls = []

    def get(url, **_kwargs):
        calls.append(url)
        return Response(url, INDEX if url == nbs.RELEASE_INDEX else RELEASE)

    monkeypatch.setattr(cr, "get", get)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: nbs.fetch_latest_pmi(config=cfg), range(2)))
    assert results[0] == results[1]
    assert results[0]["value"] == 49.2
    assert len(calls) == 2
    assert _read_json(cfg.rate_limit_root / "reuse-nbs.json")["hits"] == 2

    index_path = next((cfg.meta_root / "source_cache" / "nbs").glob("pmi_index-*.json"))
    saved = json.loads(index_path.read_text())
    saved["sha256"] = "0" * 64
    index_path.write_text(json.dumps(saved))
    assert nbs.fetch_latest_pmi(config=cfg) == results[0]
    assert len(calls) == 3  # only the damaged index is fetched

    release_path = next((cfg.meta_root / "source_cache" / "nbs").glob("pmi_release-*.json"))
    saved = json.loads(release_path.read_text())
    saved["captured_at"] = "2020-01-01T00:00:00+00:00"
    release_path.write_text(json.dumps(saved))
    assert nbs.fetch_latest_pmi(config=cfg) == results[0]
    assert len(calls) == 4  # only the stale release is fetched


def test_nbs_unparseable_page_is_not_cached_and_valid_data_survives_cache_error(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CNE_RATE_LIMIT_ROOT", str(tmp_path / "egress"))
    cfg = Config(data_root=tmp_path / "lake", source_intervals={"nbs": 0})
    calls = []

    def get(url, **_kwargs):
        calls.append(url)
        return Response(url, "<p>unrelated release</p>")

    monkeypatch.setattr(cr, "get", get)
    assert nbs.fetch_latest_pmi(config=cfg) is None
    assert nbs.fetch_latest_pmi(config=cfg) is None
    assert len(calls) == 2
    assert not list((cfg.meta_root / "source_cache" / "nbs").glob("*.json"))

    def valid_get(url, **_kwargs):
        return Response(url, INDEX if url == nbs.RELEASE_INDEX else RELEASE)

    def fail_cache(*_args, **_kwargs):
        raise OSError("cache volume full")

    monkeypatch.setattr(cr, "get", valid_get)
    monkeypatch.setattr(nbs, "write_json_atomic", fail_cache)
    assert nbs.fetch_latest_pmi(config=cfg)["value"] == 49.2
