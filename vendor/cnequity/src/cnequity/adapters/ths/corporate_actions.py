"""同花顺企业行动页面，用于显式修复历史 BJ 除权除息事件。

同花顺的 ``basic`` 页面保留了不少已经退市的北交所/新三板旧代码，且表格
直接给出 A 股除权除息日。这不是日更主源，也不应悄悄改变普通回填结果；调用方
必须显式开启 repair flag，并保留 ``source=ths`` provenance。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import math
import re
import time
from collections.abc import Callable, Mapping
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from inspect import signature

import httpx
import polars as pl

from cnequity.config import Config
from cnequity.domain.http_policy import (
    SourceCoolingDown,
    record_business_refusal,
    record_cache_reuse,
    record_http_response,
)
from cnequity.domain.market_profile import serves
from cnequity.domain.rate_limit import source_request
from cnequity.domain.symbols import parse_symbol
from cnequity.file_lock import exclusive_lock
from cnequity.storage.atomic import write_json_atomic
from cnequity.storage.raw_archive import RawArchiveError, RawPayloadArchive, begin_capture

logger = logging.getLogger(__name__)

__all__ = ["fetch_corporate_actions_ths"]


_OUTPUT_SCHEMA = {
    "symbol": pl.Utf8,
    "ex_date": pl.Date,
    "action_type": pl.Utf8,
    "cash_dividend": pl.Float64,
    "bonus_ratio": pl.Float64,
    "transfer_ratio": pl.Float64,
    "allotment_ratio": pl.Float64,
    "allotment_price": pl.Float64,
}

_URL = "https://basic.10jqka.com.cn/{code}/bonus.html"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA, "Referer": "https://basic.10jqka.com.cn/"}
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_MIN_INTERVAL = 3.0
_MAX_RETRIES = 3
_PAGE_CACHE_SECONDS = 86400
_PAGE_CACHE_VERSION = 1

# The first component is usually written as ``10送3股`` while subsequent
# components omit the repeated base, e.g. ``10转4股派4.50元``. Make the
# ``10`` prefix optional after the first match so both forms are expanded.
_PLAN_RE = re.compile(r"(?:10\s*股?\s*)?(送|转增?|派|配)\s*([0-9]+(?:\.[0-9]+)?)")
_ALLOTMENT_PRICE_RE = re.compile(r"配股(?:价|价格)\s*([0-9]+(?:\.[0-9]+)?)\s*元?")


class ThsCorporateActionsError(RuntimeError):
    """同花顺企业行动页面无法读取或无法识别。"""


def _configured_archive(
    config,
    dataset: str,
    *,
    run_id: str | None = None,
    request_scope: str | None = None,
) -> RawPayloadArchive | None:
    if config is None or not hasattr(config, "meta_root"):
        return None
    should_archive = getattr(config, "should_archive_raw", None)
    if callable(should_archive) and not should_archive(dataset):
        return None
    if not bool(getattr(config, "raw_archive_enabled", True)):
        return None
    scope = str(request_scope or f"dataset:{dataset}")
    nonce = begin_capture(config, dataset, run_id, source="ths", request_scope=scope)
    return RawPayloadArchive(
        config.meta_root,
        enabled=True,
        datasets=[dataset],
        compression=getattr(config, "raw_archive_compression", "gzip"),
        max_payload_bytes=getattr(config, "raw_archive_max_payload_bytes", None),
        capture_owner=config,
        capture_run_id=run_id,
        capture_source="ths",
        capture_scope=scope,
        capture_nonce=nonce,
    )


def _page_wire(value: object) -> bytes | None:
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, bytes):
        return value
    content = getattr(value, "content", None)
    if isinstance(content, bytearray):
        return bytes(content)
    if isinstance(content, memoryview):
        return content.tobytes()
    if isinstance(content, bytes):
        return content
    return None


def _page_html(value: object, wire: bytes | None) -> str:
    if wire is not None:
        return wire.decode("gb18030", errors="ignore")
    if isinstance(value, str):
        return value
    text = getattr(value, "text", None)
    if isinstance(text, str):
        return text
    raise RawArchiveError("THS corporate_actions page has no readable response body")


class _BonusTableParser(HTMLParser):
    """Collect cell text from every HTML table row without an lxml dependency."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._tr_depth = 0
        self._cell_open = False
        self._cell_text: list[str] = []
        self._row: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._tr_depth += 1
            if self._tr_depth == 1:
                self._row = []
        elif self._tr_depth == 1 and tag in ("td", "th"):
            self._cell_open = True
            self._cell_text = []

    def handle_data(self, data: str) -> None:
        if self._tr_depth == 1 and self._cell_open:
            self._cell_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._tr_depth == 1 and tag in ("td", "th") and self._cell_open:
            self._row.append(" ".join("".join(self._cell_text).split()))
            self._cell_open = False
            self._cell_text = []
        elif tag == "tr" and self._tr_depth:
            if self._tr_depth == 1 and self._row:
                self.rows.append(self._row)
            self._tr_depth -= 1


def _empty() -> pl.DataFrame:
    return pl.DataFrame(schema=_OUTPUT_SCHEMA)


def _parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text or text in {"--", "-", "—"}:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _parse_plan(plan: str) -> dict[str, float | None]:
    """Convert a per-10-share Chinese plan into per-share fields."""
    values: dict[str, float | None] = {
        "cash_dividend": 0.0,
        "bonus_ratio": 0.0,
        "transfer_ratio": 0.0,
        "allotment_ratio": 0.0,
        "allotment_price": None,
    }
    field_by_token = {
        "派": "cash_dividend",
        "送": "bonus_ratio",
        "转": "transfer_ratio",
        "转增": "transfer_ratio",
        "配": "allotment_ratio",
    }
    for token, raw_value in _PLAN_RE.findall(plan or ""):
        try:
            value = float(raw_value) / 10.0
        except ValueError:
            continue
        if not math.isfinite(value) or value <= 0:
            continue
        field = field_by_token[token]
        values[field] = float(values[field] or 0.0) + value
    price_match = _ALLOTMENT_PRICE_RE.search(plan or "")
    if price_match:
        try:
            price = float(price_match.group(1))
        except ValueError:
            price = None
        if price is not None and math.isfinite(price) and price > 0:
            values["allotment_price"] = price
    return values


def _rows_from_page(symbol: str, html: str, start: date, end: date) -> list[dict]:
    """Parse completed plans from one bonus page within the requested window."""
    parser = _BonusTableParser()
    parser.feed(html)
    rows: list[dict] = []
    for cells in parser.rows:
        # The current table has 11 columns. Keep the positional contract
        # anchored to the visible header: plan text is index 4, A-share ex-date
        # index 6, and progress index 8. Rows from unrelated page tables are
        # ignored by the progress/date/action checks below.
        if len(cells) < 9 or not cells[8].startswith("实施方案"):
            continue
        ex_date = _parse_date(cells[6])
        if ex_date is None or not (start <= ex_date <= end):
            continue
        values = _parse_plan(cells[4])
        parsed = {
            "cash_dividend": float(values["cash_dividend"] or 0.0),
            "bonus_ratio": float(values["bonus_ratio"] or 0.0),
            "transfer_ratio": float(values["transfer_ratio"] or 0.0),
            "allotment_ratio": float(values["allotment_ratio"] or 0.0),
            "allotment_price": values["allotment_price"],
        }
        common = {
            "symbol": symbol,
            "ex_date": ex_date,
            "cash_dividend": 0.0,
            "bonus_ratio": 0.0,
            "transfer_ratio": 0.0,
            "allotment_ratio": 0.0,
            "allotment_price": None,
        }
        if parsed["cash_dividend"] > 0:
            rows.append(
                {**common, "action_type": "cash_dividend", "cash_dividend": parsed["cash_dividend"]}
            )
        if parsed["bonus_ratio"] > 0:
            rows.append({**common, "action_type": "bonus", "bonus_ratio": parsed["bonus_ratio"]})
        if parsed["transfer_ratio"] > 0:
            rows.append(
                {**common, "action_type": "transfer", "transfer_ratio": parsed["transfer_ratio"]}
            )
        if parsed["allotment_ratio"] > 0:
            rows.append(
                {
                    **common,
                    "action_type": "allotment",
                    "allotment_ratio": parsed["allotment_ratio"],
                    "allotment_price": parsed["allotment_price"],
                }
            )
    return rows


def _is_bonus_page(html: str) -> bool:
    parser = _BonusTableParser()
    parser.feed(html)
    return any(
        any("A股除权除息日" in cell for cell in row) and any("方案进度" in cell for cell in row)
        for row in parser.rows
    )


def _cacheable_bonus_page(code: str, html: str) -> bool:
    # A structurally valid empty page does not prove that an action will never
    # appear. Recheck it later instead of caching absence as a lasting fact.
    return _is_bonus_page(html) and bool(_rows_from_page(f"{code}.BJ", html, date.min, date.max))


def _download_page_wire(code: str, *, config: Config | None) -> tuple[bytes, datetime]:
    """Return the exact GB18030 wire bytes and their actual capture time."""
    retries = config.max_retries if config is not None else _MAX_RETRIES
    backoff = float(config.retry_backoff_seconds if config is not None else 2.0)
    timeout = _DEFAULT_TIMEOUT
    last_exc: Exception | None = None
    url = _URL.format(code=code)
    for attempt in range(max(1, retries)):
        if config is None:
            time.sleep(_DEFAULT_MIN_INTERVAL)
        try:
            with source_request(config, "ths_bonus"):
                response = httpx.get(
                    url,
                    headers=_HEADERS,
                    timeout=timeout,
                    follow_redirects=True,
                )
                record_http_response(config, "ths_bonus", response)
        except SourceCoolingDown:
            raise
        except Exception as exc:  # noqa: BLE001 — retry and report symbol failure
            last_exc = exc
        else:
            if response.status_code == 200:
                wire = _page_wire(response)
                if wire is None:
                    raise ThsCorporateActionsError(f"{url} -> HTTP 200 without response content")
                return wire, datetime.now(timezone.utc)
            if response.status_code in (401, 403):
                if response.status_code == 401:
                    record_business_refusal(config, "ths_bonus", kind="public_token_gate")
                raise ThsCorporateActionsError(
                    f"{url} -> HTTP {response.status_code} (token-gated endpoint)"
                )
            if response.status_code == 404:
                raise ThsCorporateActionsError(f"{url} -> HTTP 404 (page not found)")
            last_exc = ThsCorporateActionsError(f"{url} -> HTTP {response.status_code}")
        if attempt + 1 < max(1, retries):
            time.sleep(backoff * (attempt + 1))
    raise ThsCorporateActionsError(f"{url} failed after {max(1, retries)} attempts") from last_exc


def _archive_page_wire(
    code: str,
    wire: bytes,
    captured_at: datetime,
    *,
    archive: RawPayloadArchive | None,
    run_id: str | None,
    request_scope: str | None,
    reused: bool,
) -> None:
    if archive is None or not archive.enabled:
        return
    archive.archive(
        "corporate_actions",
        wire,
        source="ths",
        request_params={"code": code},
        captured_at=captured_at,
        run_id=run_id,
        url=_URL.format(code=code),
        response_status=200,
        payload_format="bytes",
        http_metadata={"wire_exact": True, "protocol": "http", "cache_reuse": reused},
        observation_id=(
            f"{run_id or 'anonymous'}:bonus:{code}:"
            f"scope={request_scope or 'scope-unknown'}:"
            f"captured={captured_at.isoformat()}:cache={reused}"
        ),
        request_scope=request_scope,
    )


def _fetch_page(
    code: str,
    *,
    config: Config | None = None,
    archive: RawPayloadArchive | None = None,
    run_id: str | None = None,
    request_scope: str | None = None,
) -> str:
    """Fetch one page or reuse its validated bytes without refreshing evidence time."""
    url = _URL.format(code=code)
    if config is None:
        wire, captured_at = _download_page_wire(code, config=None)
        _archive_page_wire(
            code,
            wire,
            captured_at,
            archive=archive,
            run_id=run_id,
            request_scope=request_scope,
            reused=False,
        )
        html = wire.decode("gb18030", errors="ignore")
        if not _is_bonus_page(html):
            raise ThsCorporateActionsError(f"{url} -> HTTP 200 without a bonus table")
        return html

    url_key = hashlib.sha256(url.encode()).hexdigest()[:16]
    path = config.meta_root / "source_cache" / "ths_bonus" / f"{code}-{url_key}.json"
    with exclusive_lock(path.with_suffix(".lock")):
        if path.exists():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                captured_at = datetime.fromisoformat(saved["captured_at"])
                age = datetime.now(timezone.utc) - captured_at
                wire = base64.b64decode(saved["wire_b64"], validate=True)
                if (
                    saved["url"] == url
                    and saved["parser_version"] == _PAGE_CACHE_VERSION
                    and 0 <= age.total_seconds() < _PAGE_CACHE_SECONDS
                    and hashlib.sha256(wire).hexdigest() == saved["sha256"]
                    and _cacheable_bonus_page(code, wire.decode("gb18030", errors="ignore"))
                ):
                    _archive_page_wire(
                        code,
                        wire,
                        captured_at,
                        archive=archive,
                        run_id=run_id,
                        request_scope=request_scope,
                        reused=True,
                    )
                    record_cache_reuse(config, "ths_bonus", "corporate_actions_page")
                    return wire.decode("gb18030", errors="ignore")
            except (OSError, KeyError, TypeError, ValueError, binascii.Error) as exc:
                logger.warning("THS bonus page cache invalid for %s; refreshing: %s", code, exc)

        wire, captured_at = _download_page_wire(code, config=config)
        html = wire.decode("gb18030", errors="ignore")
        _archive_page_wire(
            code,
            wire,
            captured_at,
            archive=archive,
            run_id=run_id,
            request_scope=request_scope,
            reused=False,
        )
        if not _is_bonus_page(html):
            raise ThsCorporateActionsError(f"{url} -> HTTP 200 without a bonus table")
        if _cacheable_bonus_page(code, html):
            try:
                write_json_atomic(
                    path,
                    {
                        "url": url,
                        "captured_at": captured_at.isoformat(),
                        "parser_version": _PAGE_CACHE_VERSION,
                        "sha256": hashlib.sha256(wire).hexdigest(),
                        "wire_b64": base64.b64encode(wire).decode("ascii"),
                    },
                )
            except OSError as exc:
                logger.warning("THS valid bonus page for %s could not be cached: %s", code, exc)
        return html


def fetch_corporate_actions_ths(
    symbols: list[str],
    start: date,
    end: date,
    *,
    config: Config | None = None,
    symbol_windows: Mapping[str, tuple[date, date]] | None = None,
    page_fetcher: Callable[[str], str] | None = None,
    run_id: str | None = None,
    archive: RawPayloadArchive | None = None,
    request_scope: str | None = None,
) -> tuple[pl.DataFrame, list[str]]:
    """Fetch explicit per-symbol THS repairs and return ``(rows, failures)``.

    Only BJ symbols are queried. ``symbol_windows`` lets the caller bound the
    request to each instrument's actual listing/delisting interval so an old
    page cannot create events outside the symbol's observed life.
    """
    if archive is None:
        scope = request_scope or f"repair:ths:{start.isoformat()}:{end.isoformat()}"
        archive = _configured_archive(
            config,
            "corporate_actions",
            run_id=run_id,
            request_scope=scope,
        )
    if archive is not None and archive.enabled and not run_id:
        raise RawArchiveError("THS corporate_actions archive requires a non-empty run_id")

    rows: list[dict] = []
    failed: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        if symbol in seen:
            continue
        seen.add(symbol)
        try:
            info = parse_symbol(symbol)
        except ValueError:
            logger.warning("ths corporate_actions: skipping invalid symbol %s", symbol)
            continue
        if not serves("ths_delisted_actions", symbol):
            continue
        window = symbol_windows.get(symbol, (start, end)) if symbol_windows else (start, end)
        if window[0] > window[1]:
            continue
        try:
            if page_fetcher is not None:
                try:
                    parameters = signature(page_fetcher).parameters
                except (TypeError, ValueError):
                    parameters = {}
                accepts_run_id = "run_id" in parameters or any(
                    parameter.kind is parameter.VAR_KEYWORD for parameter in parameters.values()
                )
                if archive is not None and archive.enabled and not accepts_run_id:
                    raise RawArchiveError(
                        "THS corporate_actions page adapter cannot receive run_id"
                    )
                page = (
                    page_fetcher(info.code, run_id=run_id)
                    if accepts_run_id
                    else page_fetcher(info.code)
                )
                wire = _page_wire(page)
                if archive is not None and archive.enabled:
                    if wire is None:
                        raise RawArchiveError(
                            f"THS corporate_actions {symbol}: page has no exact wire bytes"
                        )
                    archive.archive(
                        "corporate_actions",
                        wire,
                        source="ths",
                        request_params={"code": info.code},
                        run_id=run_id,
                        url=_URL.format(code=info.code),
                        payload_format="bytes",
                        http_metadata={"wire_exact": True, "protocol": "http"},
                        observation_id=(
                            f"{run_id}:bonus:{info.code}:scope={request_scope or 'scope-unknown'}"
                        ),
                        request_scope=request_scope,
                    )
                html = _page_html(page, wire)
            else:
                html = _fetch_page(
                    info.code,
                    config=config,
                    archive=archive,
                    run_id=run_id,
                    request_scope=request_scope,
                )
            rows.extend(_rows_from_page(symbol, html, window[0], window[1]))
        except Exception as exc:  # noqa: BLE001 — preserve other symbols for retry
            if isinstance(exc, RawArchiveError):
                raise
            failed.append(symbol)
            logger.warning("ths corporate_actions: failed for %s: %s", symbol, exc)

    if not rows:
        return _empty(), failed
    frame = pl.DataFrame(rows, schema_overrides={"allotment_price": pl.Float64})
    return (
        frame.unique(subset=["symbol", "ex_date", "action_type"], keep="last").sort(
            ["ex_date", "symbol", "action_type"]
        ),
        failed,
    )
