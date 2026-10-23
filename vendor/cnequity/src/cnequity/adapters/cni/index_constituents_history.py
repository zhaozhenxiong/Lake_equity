"""CNI (国证指数) historical index membership — C2 backfill for SZ indices.

CSI (中证) 000300/000905/… have no free dated membership API in-repo; EM
``TRADE_DATE`` is entry-date on the *current* book, not a full as-of snapshot.
CNI publishes adjustment spreadsheets with [开始日期, 结束日期] spells that
reconstruct membership for 399001/399006 (and peers) from late 2021.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import logging
from datetime import date, datetime, timezone

import httpx
import polars as pl

from cnequity.adapters.sw.industry_history import exchange_from_code
from cnequity.domain.http_policy import record_cache_reuse, record_http_response
from cnequity.domain.rate_limit import source_request
from cnequity.domain.symbols import format_symbol, is_all_a_symbol
from cnequity.file_lock import exclusive_lock
from cnequity.storage.atomic import write_json_atomic

logger = logging.getLogger(__name__)

__all__ = [
    "CNI_ADJUST_URL",
    "CNI_BACKFILL_INDICES",
    "CniAdjustmentPayloadError",
    "fetch_cni_index_adjustments",
    "expand_cni_constituents_as_of",
]

CNI_ADJUST_URL = "https://www.cnindex.com.cn/sample-detail/download-adjustment"

# Index codes CNI serves adjustment history for (empty Excel for CSI codes).
CNI_BACKFILL_INDICES: tuple[str, ...] = ("399001.SZ", "399006.SZ")

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; cnequity/0.1)"}
_ADJUSTMENT_CACHE_SECONDS = 86400
_CACHE_PARSER_VERSION = 1


class CniAdjustmentPayloadError(RuntimeError):
    """Raised when a supported CNI workbook is empty or malformed."""


def _index_code(index_symbol: str) -> str:
    return index_symbol.split(".", 1)[0].zfill(6)


def _member_symbol(code: str) -> str | None:
    code = str(code).zfill(6)
    exchange = exchange_from_code(code)
    if not is_all_a_symbol(code, exchange):
        return None
    return format_symbol(code, exchange)


def _empty_adjustments() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "index_symbol": pl.Utf8,
            "symbol": pl.Utf8,
            "start_date": pl.Date,
            "end_date": pl.Date,
            "adjust_type": pl.Utf8,
        }
    )


def fetch_cni_index_adjustments(
    index_symbol: str,
    *,
    client: httpx.Client | None = None,
    config=None,
) -> pl.DataFrame:
    """Download CNI adjustment history for one index.

    Unsupported indices are represented by an empty frame because this endpoint
    is intentionally a narrow CNI backfill.  For an index we explicitly support,
    an empty or malformed workbook is a source failure, not an empty history;
    returning an empty frame there would let the caller advance with incomplete
    historical membership.
    """
    if index_symbol not in CNI_BACKFILL_INDICES:
        return _empty_adjustments()

    try:
        import pandas as pd  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "pandas is required to parse CNI adjustment XLSX; "
            "reinstall with `pip install --force-reinstall cnequity` "
            "(or `pip install -e .` from a source checkout)."
        ) from exc

    def download() -> bytes:
        session = (
            client if client is not None else httpx.Client(timeout=120.0, follow_redirects=True)
        )
        try:
            with source_request(config, "cni"):
                resp = session.get(
                    CNI_ADJUST_URL,
                    params={"indexcode": _index_code(index_symbol)},
                    headers=_HEADERS,
                )
                record_http_response(config, "cni", resp)
            resp.raise_for_status()
            return resp.content
        finally:
            if client is None:
                session.close()

    def parse(content: bytes) -> pl.DataFrame:
        if not content:
            raise CniAdjustmentPayloadError(f"CNI adjustment response for {index_symbol} is empty")
        missing_notice = f"{_index_code(index_symbol)}的样本历史调样文件不存在！".encode()
        if content.strip() == missing_notice:
            raise CniAdjustmentPayloadError(
                f"CNI publisher does not serve adjustment history for {index_symbol}; "
                "historical coverage is unresolved, and current constituents cannot replace it"
            )
        if len(content) < 100:
            raise CniAdjustmentPayloadError(
                f"CNI adjustment response for {index_symbol} is truncated"
            )
        try:
            pdf = pd.read_excel(io.BytesIO(content), engine="openpyxl")
        except Exception as exc:  # noqa: BLE001 — empty/corrupt payload
            raise CniAdjustmentPayloadError(
                f"CNI adjustment workbook for {index_symbol} is malformed"
            ) from exc
        return _adjustments_from_sheet(pdf, index_symbol, pd)

    # Caller-supplied clients are independent transport boundaries. Configured
    # production reads hold one per-index lock through parsing and cache write,
    # so simultaneous consumers reuse one validated file.
    if config is None or client is not None:
        return parse(download())
    url_key = hashlib.sha256(CNI_ADJUST_URL.encode()).hexdigest()[:16]
    path = (
        config.meta_root
        / "source_cache"
        / "cni"
        / f"adjustments-{_index_code(index_symbol)}-{url_key}.json"
    )
    with exclusive_lock(path.with_suffix(".lock")):
        if path.exists():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                age = datetime.now(timezone.utc) - datetime.fromisoformat(saved["captured_at"])
                raw = base64.b64decode(saved["content_b64"], validate=True)
                if (
                    saved["url"] == CNI_ADJUST_URL
                    and saved["index_symbol"] == index_symbol
                    and saved["parser_version"] == _CACHE_PARSER_VERSION
                    and 0 <= age.total_seconds() < _ADJUSTMENT_CACHE_SECONDS
                    and hashlib.sha256(raw).hexdigest() == saved["sha256"]
                ):
                    adjustments = parse(raw)
                    record_cache_reuse(config, "cni", "index_adjustments")
                    return adjustments
            except (
                OSError,
                KeyError,
                TypeError,
                ValueError,
                binascii.Error,
                CniAdjustmentPayloadError,
            ) as exc:
                logger.warning(
                    "CNI cached adjustments invalid for %s; refreshing: %s", index_symbol, exc
                )
        content = download()
        adjustments = parse(content)
        try:
            write_json_atomic(
                path,
                {
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "url": CNI_ADJUST_URL,
                    "index_symbol": index_symbol,
                    "parser_version": _CACHE_PARSER_VERSION,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "content_b64": base64.b64encode(content).decode("ascii"),
                },
            )
        except OSError as exc:
            logger.warning(
                "CNI valid adjustments for %s could not be cached: %s", index_symbol, exc
            )
        return adjustments


def _adjustments_from_sheet(pdf, index_symbol: str, pd) -> pl.DataFrame:
    if pdf.empty:
        raise CniAdjustmentPayloadError(f"CNI adjustment workbook for {index_symbol} has no rows")

    rename = {
        "开始日期": "start_date",
        "结束日期": "end_date",
        "样本代码": "code",
        "调整类型": "adjust_type",
    }
    pdf = pdf.rename(columns={k: v for k, v in rename.items() if k in pdf.columns})
    missing = {"start_date", "end_date", "code", "adjust_type"} - set(pdf.columns)
    if missing:
        raise CniAdjustmentPayloadError(
            f"CNI adjustment workbook for {index_symbol} is missing {sorted(missing)}"
        )
    for col in ("start_date", "end_date"):
        pdf[col] = pd.to_datetime(pdf[col], errors="coerce").dt.date
    rows: list[dict] = []
    for rec in pdf.to_dict(orient="records"):
        adj = str(rec.get("adjust_type") or "").strip()
        # OLD / + are in-book; '-' removals and 备选 are not active members.
        if adj not in {"OLD", "+"}:
            continue
        sym = _member_symbol(str(rec.get("code") or ""))
        start = rec.get("start_date")
        end = rec.get("end_date")
        if not sym or start is None or end is None:
            continue
        rows.append(
            {
                "index_symbol": index_symbol,
                "symbol": sym,
                "start_date": start,
                "end_date": end,
                "adjust_type": adj,
            }
        )
    if not rows:
        raise CniAdjustmentPayloadError(
            f"CNI adjustment workbook for {index_symbol} has no valid rows"
        )
    return pl.DataFrame(rows).unique(
        subset=["index_symbol", "symbol", "start_date", "end_date", "adjust_type"],
        keep="last",
        maintain_order=True,
    )


def expand_cni_constituents_as_of(
    adjustments: pl.DataFrame,
    as_of_dates: list[date],
) -> pl.DataFrame:
    """Members where start_date <= as_of < end_date for each as_of."""
    schema = {
        "index_symbol": pl.Utf8,
        "symbol": pl.Utf8,
        "as_of_date": pl.Date,
        "weight": pl.Float64,
    }
    if adjustments.is_empty() or not as_of_dates:
        return pl.DataFrame(schema=schema)
    frames: list[pl.DataFrame] = []
    for as_of in as_of_dates:
        snap = (
            adjustments.filter((pl.col("start_date") <= as_of) & (pl.col("end_date") > as_of))
            .select(
                pl.col("index_symbol"),
                pl.col("symbol"),
                pl.lit(as_of).alias("as_of_date"),
                pl.lit(None, dtype=pl.Float64).alias("weight"),
            )
            .unique(subset=["index_symbol", "symbol", "as_of_date"], keep="last")
        )
        if not snap.is_empty():
            frames.append(snap)
    if not frames:
        return pl.DataFrame(schema=schema)
    return pl.concat(frames)
