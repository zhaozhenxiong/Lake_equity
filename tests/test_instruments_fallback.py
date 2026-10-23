from __future__ import annotations

import io
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from cnequity.query import dataset_state, load_with_receipt
from lake_equity.data import instruments


@pytest.fixture
def directory_root(tmp_path):
    config = tmp_path / "configs" / "cnequity.toml"
    config.parent.mkdir()
    config.write_text('[data]\nroot=' + json.dumps((tmp_path / "var" / "lake").as_posix()) + '\n[sources.eastmoney]\nenabled=true\npush2_breaker=true\npush2_daily_budget=150\npush2_min_interval_seconds=4.0\npush2_max_concurrency=1\n[raw_archive]\nenabled=true\ndatasets=["instruments"]\n', encoding="utf-8")
    return tmp_path


def fake_directory(monkeypatch, rows, *, total=None, fail_page=None):
    class FixtureClient:
        def __init__(self, *, config, **kwargs):
            self.config = config

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            assert urlsplit(url).hostname == "push2.eastmoney.com"
            page = int(parse_qs(urlsplit(url).query)["pn"][0])
            if page == fail_page:
                raise httpx.ReadTimeout("Synthetic interrupted second directory page")
            first = (page - 1) * instruments.PAGE_SIZE
            return httpx.Response(200, json={"data": {"diff": rows[first:first + instruments.PAGE_SIZE], "total": len(rows) if total is None else total}}, request=httpx.Request("GET", url))

    monkeypatch.setattr(instruments, "_DirectoryClient", FixtureClient)


def test_complete_current_directory_publishes_partial_historical_identity(directory_root, monkeypatch):
    rows = [{"f12": "600001", "f13": 1, "f14": "Synthetic security one", "f26": 20000101}, {"f12": "000001", "f13": 0, "f14": "Synthetic security two", "f26": "-"}, {"f12": "300001", "f13": 0, "f14": "Synthetic future listing", "f26": 20990101}]
    fake_directory(monkeypatch, rows)
    report = instruments.publish_eastmoney_instruments(directory_root)
    assert report["status"] == "degraded"
    assert report["coverage_status"] == "partial"
    assert report["current_source_population_complete"] is True
    assert report["historical_population_verified"] is False
    assert report["pagination"]["page_count"] == 1
    result = load_with_receipt("instruments", data_root=directory_root / "var" / "lake", require_replayable=True)
    assert result.frame.height == 3
    assert result.frame["source"].unique().to_list() == ["eastmoney"]
    assert result.frame["list_date"].null_count() == 2
    assert result.frame["delist_date"].null_count() == 3
    assert not (directory_root / "var" / "lake" / "curated" / "daily_bars").exists()
    assert not (directory_root / "var" / "research_inputs").exists()


def test_short_directory_response_cannot_publish(directory_root, monkeypatch):
    fake_directory(monkeypatch, [{"f12": "600001", "f13": 1, "f14": "Synthetic security", "f26": 20000101}], total=2)
    with pytest.raises(RuntimeError):
        instruments.publish_eastmoney_instruments(directory_root)
    assert dataset_state("instruments", data_root=directory_root / "var" / "lake").revision_id is None
    assert list((directory_root / "var" / "lake" / "meta" / "raw" / "instruments").rglob("*.gz"))


def test_interrupted_pagination_retains_wire_without_publishing(directory_root, monkeypatch):
    rows = [{"f12": f"{600000 + number:06d}", "f13": 1, "f14": f"Synthetic security {number}", "f26": 20000101} for number in range(101)]
    fake_directory(monkeypatch, rows, fail_page=2)
    with pytest.raises(RuntimeError):
        instruments.publish_eastmoney_instruments(directory_root)
    assert dataset_state("instruments", data_root=directory_root / "var" / "lake").revision_id is None
    assert len(list((directory_root / "var" / "lake" / "meta" / "raw" / "instruments").rglob("*.gz"))) == 1


def test_absent_existing_security_is_retained_without_delisting(directory_root, monkeypatch):
    fake_directory(monkeypatch, [{"f12": "600001", "f13": 1, "f14": "Synthetic existing security", "f26": 20000101}])
    instruments.publish_eastmoney_instruments(directory_root)
    fake_directory(monkeypatch, [{"f12": "000001", "f13": 0, "f14": "Synthetic new security", "f26": 20050101}])
    report = instruments.publish_eastmoney_instruments(directory_root)
    assert report["merged_rows"] == 2
    assert report["allow_delist_inference"] is False
    result = load_with_receipt("instruments", data_root=directory_root / "var" / "lake", require_replayable=True)
    assert result.frame.height == 2
    assert result.frame["delist_date"].null_count() == 2


def test_approved_budget_cannot_be_silently_increased(directory_root, monkeypatch):
    config = directory_root / "configs" / "cnequity.toml"
    config.write_text(config.read_text().replace("push2_daily_budget=150", "push2_daily_budget=200"), encoding="utf-8")
    fake_directory(monkeypatch, [])
    with pytest.raises(ValueError, match="existing breaker"):
        instruments.publish_eastmoney_instruments(directory_root)


def test_default_archive_policy_is_explicitly_extended_for_instrument_wire(directory_root, monkeypatch):
    config = directory_root / "configs" / "cnequity.toml"
    initial = config.read_text().replace('datasets=["instruments"]\n', '')
    config.write_text(initial, encoding="utf-8")
    fake_directory(monkeypatch, [{"f12": "600001", "f13": 1, "f14": "Synthetic observed security", "f26": 20000101}])
    report = instruments.publish_eastmoney_instruments(directory_root)
    assert report["publication_status"] == "published"
    assert config.read_text() == initial


@pytest.fixture
def official_root(directory_root, monkeypatch):
    config = directory_root / "configs" / "cnequity.toml"
    config.write_text(config.read_text() + '\n[sources.exchange]\nenabled=true\nmin_interval_seconds=1.0\n', encoding="utf-8")
    monkeypatch.setattr(instruments, "OFFICIAL_MIN_ROWS", 1)
    return directory_root


def official_payloads(*, listed="2001-01-01", sz_code="000001"):
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.active.append(["A股代码", "A股简称", "A股上市日期"])
    workbook.active.append([sz_code, "Synthetic SZ stock", "2002-02-02"])
    output = io.BytesIO()
    workbook.save(output)
    return {"SH": f"公司代码\t公司简称\t上市日期\n600001\tSynthetic SH stock\t{listed}\n".encode("gbk"), "SZ": output.getvalue()}


def fake_official(monkeypatch, payloads, *, fail_market=None, status=200, short_market=None):
    calls = []

    class OfficialClient:
        @staticmethod
        def get(url, **kwargs):
            market = "SH" if url == instruments.SSE_HTTPS_URL else "SZ"
            assert url in {instruments.SSE_HTTPS_URL, instruments.st_lists.SZSE_URL}
            assert url.startswith("https://")
            assert kwargs["timeout"] == 60
            assert kwargs["allow_redirects"] is False
            assert kwargs["impersonate"] == "chrome"
            calls.append(market)
            if market == fail_market:
                raise httpx.ReadTimeout("Synthetic official directory transport failure")
            payload = payloads[market]
            length = len(payload) + (1 if market == short_market else 0)
            return httpx.Response(status, content=payload, headers={"content-length": str(length)}, request=httpx.Request("GET", url))

    monkeypatch.setattr(instruments.st_lists, "_client", lambda: OfficialClient)
    return calls


def test_complete_official_sh_sz_directory_uses_real_dates_and_source(official_root, monkeypatch):
    payloads = official_payloads()
    calls = fake_official(monkeypatch, payloads)
    report = instruments.publish_exchange_instruments(official_root)
    assert calls == ["SH", "SZ"]
    assert report["publication_status"] == "published"
    assert report["coverage_status"] == "partial"
    assert report["full_a_share_market_population_complete"] is False
    assert report["historical_population_verified"] is False
    assert report["allow_delist_inference"] is False
    assert report["excluded_markets"] == ["BJ"]
    assert len(report["directory_evidence"]) == 2
    assert any(finding["check"] == "unrouted_source" and finding["source"] == "exchange" for finding in report["findings"])
    result = load_with_receipt("instruments", data_root=official_root / "var" / "lake", require_replayable=True)
    assert result.frame.height == 2
    assert result.frame["source"].unique().to_list() == ["exchange"]
    assert result.frame["list_date"].null_count() == 0
    assert result.frame["delist_date"].null_count() == 2
    assert not (official_root / "var" / "lake" / "curated" / "daily_bars").exists()
    assert not (official_root / "var" / "research_inputs").exists()
    sidecars = list((official_root / "var" / "lake" / "meta" / "raw" / "instruments").rglob("*.json"))
    assert len(sidecars) == 2
    assert {json.loads(path.read_text())["http_metadata"]["response_envelope"] for path in sidecars} == {"tsv", "xlsx"}
    assert all(json.loads(path.read_text())["source"] == "exchange" for path in sidecars)


@pytest.mark.parametrize("market", ["SH", "SZ"])
def test_failed_official_market_stops_without_partial_master(official_root, monkeypatch, market):
    calls = fake_official(monkeypatch, official_payloads(), fail_market=market)
    with pytest.raises(httpx.ReadTimeout):
        instruments.publish_exchange_instruments(official_root)
    assert calls == (["SH"] if market == "SH" else ["SH", "SZ"])
    assert dataset_state("instruments", data_root=official_root / "var" / "lake").revision_id is None
    assert not (official_root / "var" / "lake" / "staging" / "instruments").exists()
    reports = list((official_root / "var" / "lake" / "meta" / "lake_equity" / "instrument_fallback").glob("*.json"))
    assert json.loads(reports[0].read_text())["publication_status"] == "none"
    if market == "SZ":
        assert len(list((official_root / "var" / "lake" / "meta" / "raw" / "instruments").rglob("*.gz"))) == 1


@pytest.mark.parametrize("listed", ["", "not-a-date", "2099-01-01"])
def test_official_directory_requires_actual_nonfuture_listing_dates(official_root, monkeypatch, listed):
    calls = fake_official(monkeypatch, official_payloads(listed=listed))
    with pytest.raises(ValueError):
        instruments.publish_exchange_instruments(official_root)
    assert calls == ["SH"]
    assert dataset_state("instruments", data_root=official_root / "var" / "lake").revision_id is None
    assert list((official_root / "var" / "lake" / "meta" / "raw" / "instruments").rglob("*.gz"))


def test_short_official_transport_is_archived_but_never_published(official_root, monkeypatch):
    calls = fake_official(monkeypatch, official_payloads(), short_market="SH")
    with pytest.raises(ValueError, match="transport body is incomplete"):
        instruments.publish_exchange_instruments(official_root)
    assert calls == ["SH"]
    assert dataset_state("instruments", data_root=official_root / "var" / "lake").revision_id is None


def test_official_redirect_is_not_followed_or_retried(official_root, monkeypatch):
    calls = fake_official(monkeypatch, official_payloads(), status=302)
    with pytest.raises((ValueError, httpx.HTTPStatusError)):
        instruments.publish_exchange_instruments(official_root)
    assert calls == ["SH"]
    assert dataset_state("instruments", data_root=official_root / "var" / "lake").revision_id is None


def test_official_small_export_cannot_claim_complete_market(official_root, monkeypatch):
    monkeypatch.setattr(instruments, "OFFICIAL_MIN_ROWS", 1000)
    calls = fake_official(monkeypatch, official_payloads())
    with pytest.raises(ValueError, match="unexpectedly small"):
        instruments.publish_exchange_instruments(official_root)
    assert calls == ["SH"]


def test_official_publish_preserves_absent_prior_identities(official_root, monkeypatch):
    fake_directory(monkeypatch, [{"f12": "600005", "f13": 1, "f14": "Synthetic prior identity", "f26": 20000101}])
    instruments.publish_eastmoney_instruments(official_root)
    fake_official(monkeypatch, official_payloads())
    report = instruments.publish_exchange_instruments(official_root)
    result = load_with_receipt("instruments", data_root=official_root / "var" / "lake", require_replayable=True)
    assert report["merged_rows"] == 3
    assert result.frame["delist_date"].null_count() == 3
    prior = result.frame.filter(result.frame["symbol"] == "600005.SH")
    assert prior["source"].to_list() == ["eastmoney"]


def test_sse_export_excludes_only_explicit_b_only_rows(official_root, monkeypatch):
    payloads = official_payloads()
    payloads["SH"] = "公司代码 \t公司简称 \t代码\t简称\t上市日期\t\n600001\tSynthetic A\t600001\tSynthetic A\t2001-01-01\t\n900929\tSynthetic B\t-\t-\t1994-09-28\t\n".encode("gbk")
    calls = fake_official(monkeypatch, payloads)
    report = instruments.publish_exchange_instruments(official_root)
    assert calls == ["SH", "SZ"]
    assert report["observed_rows"] == 2
    assert report["directory_evidence"][0]["rows"] == 1


def test_unexplained_sse_non_a_row_is_not_silently_dropped(official_root, monkeypatch):
    payloads = official_payloads()
    payloads["SH"] += b"900929\tSynthetic B\t1994-09-28\n"
    calls = fake_official(monkeypatch, payloads)
    with pytest.raises(ValueError, match="out-of-scope"):
        instruments.publish_exchange_instruments(official_root)
    assert calls == ["SH"]
    assert dataset_state("instruments", data_root=official_root / "var" / "lake").revision_id is None


def test_verified_sse_archive_replays_without_another_sse_request(official_root, monkeypatch):
    from cnequity.storage.raw_archive import RawPayloadArchive

    fake_official(monkeypatch, official_payloads(), fail_market="SZ")
    with pytest.raises(httpx.ReadTimeout):
        instruments.publish_exchange_instruments(official_root)
    archive = RawPayloadArchive(official_root / "var" / "lake" / "meta")
    original = archive.records("instruments")[0]
    calls = fake_official(monkeypatch, official_payloads())
    report = instruments.publish_exchange_instruments(official_root, sse_archive_metadata=original.metadata_path)
    assert calls == ["SZ"]
    evidence = report["directory_evidence"][0]
    assert evidence["payload_sha256"] == original.payload_sha256
    assert evidence["captured_at"] == original.captured_at
    assert evidence["replayed_from_metadata_path"] == original.metadata_path
    assert evidence["replayed_from_run_id"] == original.run_id
    result = load_with_receipt("instruments", data_root=official_root / "var" / "lake", require_replayable=True)
    actual_stamp = result.frame.filter(result.frame["exchange"] == "SH")["fetched_at"][0]
    assert actual_stamp.isoformat() == original.captured_at


def test_sse_replay_rejects_wrong_scope_without_network(official_root, monkeypatch):
    fake_official(monkeypatch, official_payloads(), fail_market="SZ")
    with pytest.raises(httpx.ReadTimeout):
        instruments.publish_exchange_instruments(official_root)
    sidecar = next((official_root / "var" / "lake" / "meta" / "raw" / "instruments").rglob("*.json"))
    record = json.loads(sidecar.read_text())
    record["request_scope"] = "official_current_sh_sz_equity_directory:2000-01-01"
    sidecar.write_text(json.dumps(record), encoding="utf-8")
    calls = fake_official(monkeypatch, official_payloads())
    with pytest.raises(ValueError, match="same-day"):
        instruments.publish_exchange_instruments(official_root, sse_archive_metadata=sidecar.relative_to(official_root / "var" / "lake" / "meta"))
    assert calls == []
    assert dataset_state("instruments", data_root=official_root / "var" / "lake").revision_id is None


@pytest.mark.parametrize("publisher", ["eastmoney", "exchange"])
@pytest.mark.parametrize("failure", ["progress", "quality_report"])
def test_postcommit_failure_preserves_real_publication(official_root, monkeypatch, publisher, failure):
    from cnequity.orchestrator.manifest import Manifest

    if publisher == "eastmoney":
        fake_directory(monkeypatch, [{"f12": "600001", "f13": 1, "f14": "Synthetic observed security", "f26": 20000101}])
        publish = instruments.publish_eastmoney_instruments
        expected_rows = 1
    else:
        fake_official(monkeypatch, official_payloads())
        publish = instruments.publish_exchange_instruments
        expected_rows = 2

    def observer(update):
        if update["phase"] == "published_instruments":
            raise RuntimeError("observer failure after publication")

    if failure == "quality_report":
        original_write = instruments.write_json_atomic

        def unavailable_quality_report(path, value, **kwargs):
            if path.parent.name == "findings":
                raise OSError("quality report unavailable after publication")
            return original_write(path, value, **kwargs)

        monkeypatch.setattr(instruments, "write_json_atomic", unavailable_quality_report)
    with pytest.raises(RuntimeError if failure == "progress" else OSError, match="after publication"):
        publish(official_root, progress=observer if failure == "progress" else None)
    state = dataset_state("instruments", data_root=official_root / "var" / "lake")
    assert state.revision_id is not None
    result = load_with_receipt("instruments", data_root=official_root / "var" / "lake", require_replayable=True)
    assert result.frame.height == expected_rows
    report_path = official_root / "var" / "lake" / "meta" / "lake_equity" / "instrument_fallback" / f"{state.run_id}.json"
    report = json.loads(report_path.read_text())
    assert report["status"] == "published_followup_failed"
    assert report["publication_status"] == "published"
    assert report["coverage_status"] == "partial"
    assert report["revision_id"] == state.revision_id
    assert report["observed_rows"] == expected_rows
    assert "after publication" in report["error"]
    manifest = Manifest(official_root / "var" / "lake" / "meta" / "manifest.db")
    run = dict(manifest.get_run(state.run_id))
    assert run["status"] == "failed"
    assert run["publication_status"] == "published"
    assert run["rows_written"] == expected_rows
    assert json.loads(run["metadata_json"])["status"] == "published_followup_failed"
    receipts = {row["stage"]: dict(row) for row in manifest.get_dataset_results(state.run_id)}
    assert receipts["fetch"]["status"] == "warning"
    assert receipts["publish_revision"]["revision_id"] == state.revision_id
    assert receipts["publish_revision"]["publication_status"] == "published"
    assert receipts["audit"]["reason_code"] == "published_followup_failed"
    assert receipts["audit"]["publication_status"] == "published"


@pytest.mark.parametrize("publisher", ["eastmoney", "exchange"])
def test_identical_directory_reports_unchanged_existing_revision(official_root, monkeypatch, publisher):
    from cnequity.orchestrator.manifest import Manifest

    if publisher == "eastmoney":
        fake_directory(monkeypatch, [{"f12": "600001", "f13": 1, "f14": "Synthetic observed security", "f26": 20000101}])
        publish = instruments.publish_eastmoney_instruments
    else:
        fake_official(monkeypatch, official_payloads())
        publish = instruments.publish_exchange_instruments
    first = publish(official_root)
    second = publish(official_root)
    assert first["publication_status"] == "published"
    assert second["publication_status"] == "unchanged"
    assert second["revision_id"] == first["revision_id"]
    assert second["revision"] == first["revision"]
    assert second["run_id"] != first["run_id"]
    manifest = Manifest(official_root / "var" / "lake" / "meta" / "manifest.db")
    run = manifest.get_run(second["run_id"])
    assert run["publication_status"] == "unchanged"
    receipts = {row["stage"]: dict(row) for row in manifest.get_dataset_results(second["run_id"])}
    assert receipts["publish_revision"]["publication_status"] == "unchanged"
    assert receipts["publish_revision"]["revision_id"] == first["revision_id"]


@pytest.mark.parametrize("publisher", ["eastmoney", "exchange"])
def test_pointer_published_before_commit_returns_is_not_reported_as_unpublished(official_root, monkeypatch, publisher):
    from cnequity.orchestrator.manifest import Manifest

    if publisher == "eastmoney":
        fake_directory(monkeypatch, [{"f12": "600001", "f13": 1, "f14": "Synthetic observed security", "f26": 20000101}])
        publish = instruments.publish_eastmoney_instruments
        expected_rows = 1
    else:
        fake_official(monkeypatch, official_payloads())
        publish = instruments.publish_exchange_instruments
        expected_rows = 2
    original_commit = instruments.RevisionStore.commit

    def published_pointer_then_failed_cache(store, *args, **kwargs):
        revision = original_commit(store, *args, **kwargs)
        assert revision is not None
        raise OSError("post-pointer state-cache failure")

    monkeypatch.setattr(instruments.RevisionStore, "commit", published_pointer_then_failed_cache)
    with pytest.raises(OSError, match="post-pointer state-cache failure"):
        publish(official_root)
    state = dataset_state("instruments", data_root=official_root / "var" / "lake")
    assert state.revision_id is not None
    report_path = official_root / "var" / "lake" / "meta" / "lake_equity" / "instrument_fallback" / f"{state.run_id}.json"
    report = json.loads(report_path.read_text())
    assert report["publication_status"] == "published"
    assert report["status"] == "published_followup_failed"
    assert report["revision_id"] == state.revision_id
    assert report["observed_rows"] == expected_rows
    assert report["merged_rows"] == expected_rows
    assert report["publication_recovered_from_pointer"] is True
    assert "post-pointer state-cache failure" in report["error"]
    manifest = Manifest(official_root / "var" / "lake" / "meta" / "manifest.db")
    run = dict(manifest.get_run(state.run_id))
    assert run["publication_status"] == "published"
    assert run["rows_written"] == expected_rows
    receipts = {row["stage"]: dict(row) for row in manifest.get_dataset_results(state.run_id)}
    assert receipts["fetch"]["status"] == "warning"
    assert receipts["publish_revision"]["revision_id"] == state.revision_id
    assert receipts["audit"]["reason_code"] == "published_followup_failed"


@pytest.mark.parametrize("publisher", ["eastmoney", "exchange"])
def test_failed_new_run_does_not_claim_another_runs_published_pointer(official_root, monkeypatch, publisher):
    from cnequity.orchestrator.manifest import Manifest

    if publisher == "eastmoney":
        fake_directory(monkeypatch, [{"f12": "600001", "f13": 1, "f14": "Synthetic observed security", "f26": 20000101}])
        publish = instruments.publish_eastmoney_instruments
    else:
        fake_official(monkeypatch, official_payloads())
        publish = instruments.publish_exchange_instruments
    first = publish(official_root)

    def fail_before_publication(*args, **kwargs):
        raise OSError("failure before any new commit")

    monkeypatch.setattr(instruments, "_publish_directory", fail_before_publication)
    with pytest.raises(OSError, match="before any new commit"):
        publish(official_root)
    assert dataset_state("instruments", data_root=official_root / "var" / "lake").revision_id == first["revision_id"]
    manifest = Manifest(official_root / "var" / "lake" / "meta" / "manifest.db")
    latest_run = dict(manifest.list_runs()[0])
    assert latest_run["run_id"] != first["run_id"]
    assert latest_run["publication_status"] == "none"
    metadata = json.loads(latest_run["metadata_json"])
    assert metadata["publication_status"] == "none"
    assert "revision_id" not in metadata
