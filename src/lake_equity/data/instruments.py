from __future__ import annotations

import csv
import io
import json
import math
import zipfile
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import polars as pl

from cnequity.adapters.eastmoney.clist import fetch_clist_pages
from cnequity.adapters.eastmoney.common import ALL_A_FS, PUSH2_CLIST_HOSTS, symbol_from_clist
from cnequity.adapters.eastmoney.em_auth import EastMoneyClient
from cnequity.adapters.eastmoney.instruments import _parse_list_date
from cnequity.adapters.eastmoney.raw import configured_archive
from cnequity.adapters.exchange import st_lists
from cnequity.adapters.exchange.etf_profiles import _workbook_records
from cnequity.config import load_config
from cnequity.domain.contracts import contract_fingerprint
from cnequity.domain.datasets import DATASETS
from cnequity.domain.http_policy import record_http_response
from cnequity.domain.market_time import shanghai_today
from cnequity.domain.rate_limit import source_request
from cnequity.domain.schemas import INSTRUMENTS_SCHEMA, data_version_for
from cnequity.domain.symbols import is_all_a_symbol
from cnequity.file_lock import lake_mutation_lock
from cnequity.orchestrator.manifest import Manifest
from cnequity.orchestrator.run_lock import run_lock
from cnequity.steps.http_common import verify_raw_archive, write_fetched
from cnequity.storage.atomic import write_json_atomic
from cnequity.storage.instruments import compact_instruments
from cnequity.storage.raw_archive import RawPayloadArchive
from cnequity.storage.revisions import RevisionStore, sha256_file

from lake_equity.data.service import DataOperationCancelled


FIELDS = "f12,f13,f14,f26"
PAGE_SIZE = 100
OFFICIAL_MIN_ROWS = 1000
SSE_HTTPS_URL = st_lists.SSE_URL.replace("http://", "https://", 1)


def _cancel(check_cancel):
    if check_cancel is not None and check_cancel():
        raise DataOperationCancelled("Instrument directory capture cancelled")


class _DirectoryClient(EastMoneyClient):
    def __init__(self, *, config, progress=None, check_cancel=None):
        super().__init__(config=config)
        self.progress = progress
        self.check_cancel = check_cancel
        self.pages = 0

    def get(self, url, **kwargs):
        _cancel(self.check_cancel)
        if urlsplit(url).hostname != urlsplit(PUSH2_CLIST_HOSTS[0]).hostname:
            raise ValueError("Instrument fallback permits only the configured primary push2 host")
        response = super().get(url, **kwargs)
        self.pages += 1
        if self.progress is not None:
            self.progress({"phase": "fetch_instruments", "pages": self.pages, "host": PUSH2_CLIST_HOSTS[0]})
        return response


def _instrument_frame(rows: list[dict], captured_at: datetime, on: date) -> pl.DataFrame:
    normalized = []
    seen = set()
    for row in rows:
        symbol = symbol_from_clist(str(row.get("f12", "")), row.get("f13"))
        if not symbol or symbol in seen:
            raise ValueError("Current instrument snapshot contains invalid or duplicate security identities")
        seen.add(symbol)
        listed = _parse_list_date(row.get("f26"))
        name = row.get("f14")
        if not isinstance(name, str) or not name.strip() or name == "-":
            raise ValueError("Current instrument snapshot is missing an observed security name")
        normalized.append({"symbol": symbol, "name": name.strip(), "exchange": symbol.rsplit(".", 1)[1], "asset_type": "stock", "list_date": listed if listed is not None and listed <= on else None, "delist_date": None, "prev_symbol": None, "source": "eastmoney", "data_version": data_version_for("instruments"), "fetched_at": captured_at})
    if not normalized:
        raise ValueError("Current instrument snapshot is empty")
    return pl.DataFrame(normalized, schema=INSTRUMENTS_SCHEMA)


def _pagination_evidence(config, evidence, row_count: int) -> dict:
    pages = []
    for metadata_path, payload_sha in evidence.record_keys:
        path = config.meta_root / metadata_path
        metadata = json.loads(path.read_text(encoding="utf-8"))
        parameters = metadata.get("request_params", {})
        pagination = metadata.get("pagination", {})
        if parameters.get("host") != PUSH2_CLIST_HOSTS[0] or parameters.get("fs") != ALL_A_FS or parameters.get("fields") != FIELDS or pagination.get("page_size") != PAGE_SIZE:
            raise ValueError("Instrument wire evidence does not match the approved host, fields and scope")
        pages.append({"page": pagination.get("page"), "reported_total": pagination.get("reported_total"), "metadata_path": metadata_path, "payload_sha256": payload_sha})
    totals = {page["reported_total"] for page in pages}
    if len(totals) != 1 or totals != {row_count}:
        raise ValueError("Instrument pagination does not prove the complete reported current population")
    expected_pages = math.ceil(row_count / PAGE_SIZE)
    if sorted(page["page"] for page in pages) != list(range(1, expected_pages + 1)):
        raise ValueError("Instrument snapshot has missing, duplicate or incomplete pages")
    return {"reported_total": row_count, "page_count": len(pages), "page_size": PAGE_SIZE, "host": PUSH2_CLIST_HOSTS[0], "fields": FIELDS, "fs": ALL_A_FS, "pages": sorted(pages, key=lambda page: page["page"])}


def _publish_directory(config, manifest, run_id, report, frame, evidence, on, check_cancel, progress):
    source = report["source"]
    manifest.record_dataset_result(run_id, "instruments", "fetch", "warning", rows_written=frame.height, coverage_status="partial", publication_status="none", reason_code="current_observed_only", usable_result=True)
    staged = write_fetched(config, run_id, "instruments", frame, source=source, raw_archive_evidence=evidence, preserve_fetched_at=True)
    if staged.get("rows_rejected") or staged.get("rows_written") != frame.height:
        raise ValueError("Instrument publication requires every observed directory row to pass staging")
    store = RevisionStore(config.meta_root, config.curated_root, config.derived_root)
    with lake_mutation_lock(config.meta_root, blocking=True):
        _cancel(check_cancel)
        base = store.ensure_current("instruments", _locked=True)
        changed = []
        merged_rows, findings = compact_instruments(config.staging_root, config.curated_root, run_id, on, changed_files=changed, base_root=base, allow_delist_inference=False)
        findings = [*findings, *report.get("findings", [])]
        _cancel(check_cancel)
        metadata = {**report, "current_source_population_complete": True, "observed_rows": frame.height, "retained_rows": merged_rows - frame.height, "findings": findings}
        revision = store.commit("instruments", run_id=run_id, changed_files=changed, schema_version=DATASETS["instruments"].schema_version, contract_fingerprint=contract_fingerprint("instruments"), metadata=metadata, _locked=True)
        publication = "published" if revision is not None else "unchanged"
        if revision is None:
            revision = store.latest("instruments")
        if revision is None:
            raise ValueError("Instrument fallback did not produce a committed revision")
    report.update(status="degraded", execution_status="completed", publication_status=publication, current_source_population_complete=True, observed_rows=frame.height, merged_rows=merged_rows, revision_id=revision.revision_id, revision=revision.revision, findings=findings)
    manifest.record_dataset_result(run_id, "instruments", "publish_revision", "warning", revision_id=revision.revision_id, rows_written=frame.height, execution_status="completed", coverage_status="partial", publication_status=publication, reason_code="historical_population_not_verified", usable_result=True)
    manifest.update_run_metadata(run_id, report)
    manifest.finish_run(run_id, "degraded", rows_read=frame.height, rows_written=frame.height)
    write_json_atomic(config.meta_root / "lake_equity" / "instrument_fallback" / f"{run_id}.json", report)
    write_json_atomic(config.meta_root / "quality" / "findings" / f"{run_id}.json", {"run_id": run_id, "trade_date": on.isoformat(), "findings": [*findings, {"dataset": "instruments", "severity": "warning", "check": "instruments_current_observed_only", "message": f"{source} directory is complete only within its observed current source scope; historical population, other markets and fund coverage remain unverified.", "observed_rows": frame.height}]})
    if progress is not None:
        progress({"phase": "published_instruments", "run_id": run_id, "rows": frame.height, "revision_id": revision.revision_id})
    return report


def _record_directory_failure(config, manifest, run_id, report, error, check_cancel):
    error_message = f"{type(error).__name__}: {error}"
    publication = report.get("publication_status")
    has_revision = publication in {"published", "unchanged"} and report.get("revision_id")
    if not has_revision:
        try:
            store = RevisionStore(config.meta_root, config.curated_root, config.derived_root, create=False)
            pointer = store.current_pointer("instruments")
            revision = store.latest("instruments") if pointer is not None else None
            if revision is not None and revision.run_id == run_id:
                generation = store.current_root("instruments", revision=revision.revision_id)
                if generation is None or not revision.generation_files:
                    raise ValueError("Published instrument receipt has no verifiable generation")
                for item in revision.generation_files:
                    relative = Path(item.path)
                    if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != ("instruments",):
                        raise ValueError("Published instrument receipt has an unsafe generation file")
                    path = generation.joinpath(*relative.parts[1:])
                    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(generation.resolve()) or path.stat().st_size != item.size_bytes or sha256_file(path) != item.sha256:
                        raise ValueError("Published instrument generation does not match its receipt")
                metadata = revision.metadata
                report.update(publication_status="published", revision_id=revision.revision_id, revision=revision.revision, observed_rows=metadata["observed_rows"], merged_rows=metadata["observed_rows"] + metadata["retained_rows"], current_source_population_complete=True, findings=metadata.get("findings", []), publication_recovered_from_pointer=True)
                publication = "published"
                has_revision = True
        except Exception as verification_error:
            report["publication_verification_error"] = f"{type(verification_error).__name__}: {verification_error}"
    rows = report["observed_rows"] if has_revision else 0
    if has_revision:
        reason = f"{publication}_followup_failed"
        report.update(status=reason, execution_status="failed", reason_code=reason, error=error_message)
        manifest.record_dataset_result(run_id, "instruments", "publish_revision", "warning", revision_id=report["revision_id"], rows_written=rows, execution_status="completed", coverage_status=report["coverage_status"], publication_status=publication, reason_code="historical_population_not_verified", usable_result=True)
        manifest.record_dataset_result(run_id, "instruments", "audit", "failed", revision_id=report["revision_id"], error_code=type(error).__name__, error_message=str(error), execution_status="failed", coverage_status=report["coverage_status"], publication_status=publication, reason_code=reason, usable_result=True)
    else:
        cancelled = isinstance(error, DataOperationCancelled) or check_cancel is not None and check_cancel()
        report.update(status="interrupted" if cancelled else "failed", execution_status="interrupted" if cancelled else "failed", publication_status="none", error=error_message)
        manifest.record_dataset_result(run_id, "instruments", "fetch", "failed", error_code=type(error).__name__, error_message=str(error), coverage_status="partial", publication_status="none")
    manifest.update_run_metadata(run_id, report)
    manifest.finish_run(run_id, "failed", rows_read=rows, rows_written=rows, error_message=str(error))
    write_json_atomic(config.meta_root / "lake_equity" / "instrument_fallback" / f"{run_id}.json", report)


def publish_eastmoney_instruments(project_root: str | Path, *, config_path: str | Path | None = None, progress: Callable | None = None, check_cancel: Callable | None = None) -> dict:
    root = Path(project_root).resolve()
    config = load_config(config_path or root / "configs" / "cnequity.toml")
    if not config.data_root.resolve().is_relative_to(root / "var"):
        raise ValueError("Instrument publication must stay inside the new project's var directory")
    if not config.sources.get("eastmoney", True) or not config.eastmoney_push2_breaker or not 0 < config.eastmoney_push2_daily_budget <= 150 or config.source_intervals.get("eastmoney_push2", 0) < 4 or config.source_concurrency_for("eastmoney_push2") != 1:
        raise ValueError("Instrument fallback requires enabled EastMoney, the existing breaker, <=150 daily requests and a four-second single-request lane")
    if not config.raw_archive_enabled:
        raise ValueError("Exact raw archive must be enabled for the instrument fallback")
    config.raw_archive_datasets = sorted({*config.raw_archive_datasets, "instruments"})
    _cancel(check_cancel)
    manifest = Manifest(config.manifest_path)
    captured_at = datetime.now(timezone.utc)
    on = shanghai_today()
    scope = "current_equity_directory:" + on.isoformat()
    report = {"dataset": "instruments", "adapter": "lake_equity.eastmoney_current_directory_v1", "source": "eastmoney", "scope": "current_observed_only", "coverage_status": "partial", "historical_completeness": "not_assessed", "historical_population_verified": False, "allow_delist_inference": False, "captured_at": captured_at.isoformat()}
    with run_lock(config.meta_root, "lake_equity_instruments_fallback"):
        run_id = manifest.start_run("lake_equity:instruments_fallback", report)
        report["run_id"] = run_id
        try:
            archive = configured_archive(config, "instruments", run_id=run_id, request_scope=scope)
            if archive is None:
                raise ValueError("Exact raw archive must be enabled for the instrument fallback")
            with _DirectoryClient(config=config, progress=progress, check_cancel=check_cancel) as client:
                rows = fetch_clist_pages(client, fields=FIELDS, fs=ALL_A_FS, page_size=PAGE_SIZE, archive=archive, archive_dataset="instruments", archive_run_id=run_id)
            _cancel(check_cancel)
            evidence = verify_raw_archive(config, "instruments", run_id, source="eastmoney", request_scope=scope)
            pagination = _pagination_evidence(config, evidence, len(rows))
            frame = _instrument_frame(rows, captured_at, on)
            report["pagination"] = pagination
            return _publish_directory(config, manifest, run_id, report, frame, evidence, on, check_cancel, progress)
        except BaseException as error:
            _record_directory_failure(config, manifest, run_id, report, error, check_cancel)
            raise


def _exchange_frame(payload: bytes, market: str, captured_at: datetime, on: date) -> pl.DataFrame:
    if market == "SZ":
        with zipfile.ZipFile(io.BytesIO(payload)) as workbook:
            if workbook.testzip() is not None:
                raise ValueError("SZSE directory workbook failed its CRC check")
        records = _workbook_records(payload)
        columns = ("A股代码", "A股简称", "A股上市日期")
    elif market == "SH":
        text = payload.decode("utf-8-sig" if payload.startswith(b"\xef\xbb\xbf") else "gb18030")
        reader = csv.DictReader(io.StringIO(text), delimiter="\t")
        reader.fieldnames = [column.strip() for column in reader.fieldnames or []]
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError("SSE directory has missing or duplicate headers")
        records = list(reader)
        aliases = (("证券代码", "公司代码", "A股代码", "股票代码"), ("证券简称", "公司简称", "A股简称", "股票简称"), ("上市日期", "A股上市日期"))
        columns = tuple(next((column for column in candidates if column in reader.fieldnames), "") for candidates in aliases)
    else:
        raise ValueError("Official directory market must be SH or SZ")
    if not records or not all(columns) or not set(columns) <= set(records[0]):
        raise ValueError(f"{market} directory is missing actual stock identity or listing-date columns")
    normalized = []
    seen = set()
    for record in records:
        code, name, listed = (str(record.get(column) or "").strip() for column in columns)
        if market == "SH" and code.startswith("900") and len(code) == 6 and code.isdigit() and str(record.get("代码", "")).strip() == "-" and str(record.get("简称", "")).strip() == "-":
            continue
        if len(code) != 6 or not code.isdigit() or not is_all_a_symbol(code, market) or code in seen or not name or name == "-":
            raise ValueError(f"{market} directory contains invalid, duplicate or out-of-scope security identities")
        if market == "SH" and "代码" in record and str(record["代码"]).strip() != code:
            raise ValueError("SSE company identity does not match its explicit A-share code")
        seen.add(code)
        listed = date.fromisoformat(listed)
        if listed > on:
            raise ValueError(f"{market} directory contains an unverified future listing date")
        normalized.append({"symbol": f"{code}.{market}", "name": name, "exchange": market, "asset_type": "stock", "list_date": listed, "delist_date": None, "prev_symbol": None, "source": "exchange", "data_version": data_version_for("instruments"), "fetched_at": captured_at})
    if len(normalized) < OFFICIAL_MIN_ROWS:
        raise ValueError(f"{market} official directory is unexpectedly small for a complete current-market export")
    return pl.DataFrame(normalized, schema=INSTRUMENTS_SCHEMA)


def _capture_exchange_directory(config, archive, run_id, scope, market, captured_at, on, progress, check_cancel, replay_metadata=None):
    _cancel(check_cancel)
    url, headers = (SSE_HTTPS_URL, st_lists._SSE_HEADERS) if market == "SH" else (st_lists.SZSE_URL, st_lists._SZSE_HEADERS)
    inherited = {}
    observed_at = captured_at
    if replay_metadata is not None:
        reader = RawPayloadArchive(config.meta_root)
        original = reader.record(replay_metadata)
        if market != "SH" or original.dataset != "instruments" or original.source != "exchange" or original.url != url or original.request_scope != scope or original.response_status != 200 or original.request_params != {"exchange": "SH", "one_shot_export": True} or original.http_metadata.get("wire_exact") is not True:
            raise ValueError("SSE replay requires the same-day verified official SH directory observation")
        payload = reader.read(original)
        observed_at = datetime.fromisoformat(original.captured_at)
        if observed_at > captured_at:
            raise ValueError("SSE replay cannot use a future observation")
        status = original.response_status
        response_headers = {"content-type": original.http_metadata.get("content_type"), "content-length": original.http_metadata.get("content_length"), "content-encoding": original.http_metadata.get("content_encoding")}
        inherited = {"replayed_from_metadata_path": original.metadata_path, "replayed_from_run_id": original.run_id, "replayed_from_payload_sha256": original.payload_sha256}
    else:
        if progress is not None:
            progress({"phase": "fetch_official_instruments", "market": market, "url": url, "timeout_seconds": 60})
        with source_request(config, "exchange"):
            response = st_lists._client().get(url, headers=headers, impersonate="chrome", timeout=60, allow_redirects=False)
            record_http_response(config, "exchange", response)
        payload = response.content
        status = response.status_code
        response_headers = response.headers
    record = archive.archive("instruments", payload, source="exchange", request_params={"exchange": market, "one_shot_export": True}, captured_at=observed_at, run_id=run_id, url=url, response_status=status, payload_format="bytes", http_metadata={"wire_exact": True, "response_envelope": "tsv" if market == "SH" else "xlsx", "content_type": response_headers.get("content-type"), "content_length": response_headers.get("content-length"), "content_encoding": response_headers.get("content-encoding"), **inherited}, observation_id=f"{run_id}:exchange:{market}", request_scope=scope)
    if status != 200:
        raise ValueError(f"{market} official export did not return HTTP 200; redirects are not followed")
    content_length = response_headers.get("content-length")
    if content_length and not response_headers.get("content-encoding") and int(content_length) != len(payload):
        raise ValueError(f"{market} official directory transport body is incomplete")
    frame = _exchange_frame(payload, market, observed_at, on)
    _cancel(check_cancel)
    return frame, {"market": market, "url": url, "payload_sha256": sha256(payload).hexdigest(), "payload_bytes": len(payload), "metadata_path": record.metadata_path, "captured_at": observed_at.isoformat(), "rows": frame.height, "listing_date_min": frame["list_date"].min().isoformat(), "listing_date_max": frame["list_date"].max().isoformat(), "completeness_basis": "single_official_export_complete_transport_and_validated_rows", **inherited}


def publish_exchange_instruments(project_root: str | Path, *, config_path: str | Path | None = None, sse_archive_metadata: str | Path | None = None, progress: Callable | None = None, check_cancel: Callable | None = None) -> dict:
    root = Path(project_root).resolve()
    config = load_config(config_path or root / "configs" / "cnequity.toml")
    if not config.data_root.resolve().is_relative_to(root / "var"):
        raise ValueError("Instrument publication must stay inside the new project's var directory")
    if not config.sources.get("exchange", True) or config.source_intervals.get("exchange", 0) < 1:
        raise ValueError("Official instrument fallback requires enabled exchange requests and existing pacing")
    if not config.raw_archive_enabled:
        raise ValueError("Exact raw archive must be enabled for the instrument fallback")
    config.raw_archive_datasets = sorted({*config.raw_archive_datasets, "instruments"})
    _cancel(check_cancel)
    captured_at = datetime.now(timezone.utc)
    on = shanghai_today()
    scope = "official_current_sh_sz_equity_directory:" + on.isoformat()
    spec = DATASETS["instruments"]
    declared_sources = {spec.primary_source, spec.backup_source, spec.backfill_source, *spec.supplementary_sources, *spec.repair_sources}
    findings = [] if "exchange" in declared_sources else [{"dataset": "instruments", "severity": "warning", "check": "unrouted_source", "source": "exchange", "message": "Official exchange stock directory is an explicit Lake Equity fallback not declared in the upstream instruments source routes; provenance remains exchange, not tdx_protocol."}]
    report = {"dataset": "instruments", "adapter": "lake_equity.exchange_current_directory_v1", "source": "exchange", "scope": "current_observed_only", "markets": ["SH", "SZ"], "excluded_markets": ["BJ"], "asset_scope": "current_sh_sz_a_share_equities", "scope_filter": "exclude_only_explicit_sse_900xxx_b_only_rows_with_absent_a_code_and_name", "coverage_status": "partial", "historical_completeness": "not_assessed", "historical_population_verified": False, "full_a_share_market_population_complete": False, "allow_delist_inference": False, "captured_at": captured_at.isoformat(), "findings": findings, "directory_evidence": []}
    manifest = Manifest(config.manifest_path)
    with run_lock(config.meta_root, "lake_equity_instruments_fallback"):
        run_id = manifest.start_run("lake_equity:official_instruments_fallback", report)
        report["run_id"] = run_id
        try:
            archive = configured_archive(config, "instruments", run_id=run_id, request_scope=scope, source="exchange")
            if archive is None:
                raise ValueError("Exact raw archive must be enabled for the instrument fallback")
            frames = []
            for market in ("SH", "SZ"):
                frame, metadata = _capture_exchange_directory(config, archive, run_id, scope, market, captured_at, on, progress, check_cancel, sse_archive_metadata if market == "SH" else None)
                frames.append(frame)
                report["directory_evidence"].append(metadata)
            evidence = verify_raw_archive(config, "instruments", run_id, source="exchange", request_scope=scope)
            if len(evidence.record_keys) != 2:
                raise ValueError("Official directory publication requires both complete market exports")
            return _publish_directory(config, manifest, run_id, report, pl.concat(frames), evidence, on, check_cancel, progress)
        except BaseException as error:
            _record_directory_failure(config, manifest, run_id, report, error, check_cancel)
            raise
