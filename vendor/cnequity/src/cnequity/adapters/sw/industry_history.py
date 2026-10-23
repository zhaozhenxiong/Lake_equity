"""Shenwan (申万) industry classification history — C2 backfill source.

Source: SwClass2021 ``StockClassifyUse_stock.xls`` (interval rows with 计入日期).
Daily EastMoney industry snapshots stay ``classification_system=eastmoney``;
historical rows use ``classification_system=sw``.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import io
import json
import logging
import ssl
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import polars as pl

from cnequity.domain.http_policy import record_cache_reuse, record_http_response
from cnequity.domain.rate_limit import source_request
from cnequity.domain.symbols import format_symbol, infer_exchange_from_code, is_all_a_symbol
from cnequity.file_lock import exclusive_lock
from cnequity.storage.atomic import write_json_atomic

logger = logging.getLogger(__name__)

__all__ = [
    "SW_INDUSTRY_XLS_URL",
    "exchange_from_code",
    "fetch_sw_industry_intervals",
    "expand_sw_industry_as_of",
    "sw_ssl_context",
]

SW_INDUSTRY_XLS_URL = (
    "https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls"
)

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; cnequity/0.1)"}
_HISTORY_CACHE_SECONDS = 86400

# swsresearch.com serves its leaf certificate and nothing else — measured over
# six handshakes, the chain is one cert deep every time. Browsers and macOS
# curl paper over that by fetching the issuer named in the leaf's Authority
# Information Access extension; Python does not, so certifi cannot build a path
# to a root it does trust and every request fails with
# "unable to get local issuer certificate". Measured: httpx 0/5, curl_cffi 0/6.
#
# The missing link is a public DigiCert intermediate (GeoTrust G2 TLS CN
# RSA4096 SHA256 2022 CA1, valid to 2032-12-14) whose own issuer — DigiCert
# Global Root G2 — is already in certifi. Shipping it restores the path without
# weakening anything: the root must still be trusted, the chain must still
# verify, and the hostname must still match. `verify=False` would have been the
# one-line alternative and would have turned a server misconfiguration into a
# silently unauthenticated download of the classification history.
#
# When swsresearch rotates CAs this file goes stale and the same error returns.
# Refresh it from the leaf's AIA URL:
#   openssl s_client -connect www.swsresearch.com:443 | openssl x509 -noout -text \
#     | grep -A2 "Authority Information Access"
_SW_INTERMEDIATE_PEM = Path(__file__).with_name("geotrust_g2_tls_cn_rsa4096.pem")


@functools.lru_cache(maxsize=1)
def sw_ssl_context() -> ssl.SSLContext:
    """Default trust store plus the intermediate swsresearch.com omits."""
    ctx = httpx.create_ssl_context()
    try:
        ctx.load_verify_locations(cafile=str(_SW_INTERMEDIATE_PEM))
    except (OSError, ssl.SSLError) as exc:  # pragma: no cover — packaging slip
        logger.warning("Shenwan intermediate cert unusable (%s); TLS may fail", exc)
    return ctx


def sw_client(timeout: float = 120.0) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=True, verify=sw_ssl_context())


def exchange_from_code(code: str) -> str:
    return infer_exchange_from_code(code)


def _code_to_symbol(code: str) -> str | None:
    code = str(code).zfill(6)
    exchange = exchange_from_code(code)
    if not is_all_a_symbol(code, exchange):
        return None
    return format_symbol(code, exchange)


def fetch_sw_industry_intervals(*, client: httpx.Client | None = None, config=None) -> pl.DataFrame:
    """Download Shenwan stock→industry interval history (one row per classification spell)."""
    try:
        import pandas as pd  # noqa: PLC0415 — optional; Excel parse only
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "pandas is required to parse Shenwan industry XLS; "
            "reinstall with `pip install --force-reinstall cnequity` "
            "(or `pip install -e .` from a source checkout)."
        ) from exc

    def parse(content: bytes) -> pl.DataFrame:
        pdf = pd.read_excel(
            io.BytesIO(content),
            dtype={"股票代码": str, "行业代码": str},
        )
        return _intervals_from_sheet(pdf, pd)

    def download() -> bytes:
        session = client if client is not None else sw_client()
        try:
            with source_request(config, "sw"):
                resp = session.get(SW_INDUSTRY_XLS_URL, headers=_HEADERS)
                record_http_response(config, "sw", resp)
            resp.raise_for_status()
            return resp.content
        finally:
            if client is None:
                session.close()

    # Caller-owned transports are isolated test/integration boundaries. For
    # configured production fetches, lock through validation so simultaneous
    # consumers cannot each download the same large file.
    if config is None or client is not None:
        return parse(download())
    url_hash = hashlib.sha256(SW_INDUSTRY_XLS_URL.encode()).hexdigest()[:16]
    path = config.meta_root / "source_cache" / "sw" / f"history-{url_hash}.json"
    with exclusive_lock(path.with_suffix(".lock")):
        if path.exists():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                age = datetime.now(timezone.utc) - datetime.fromisoformat(saved["captured_at"])
                raw = base64.b64decode(saved["content_b64"], validate=True)
                if (
                    saved["url"] == SW_INDUSTRY_XLS_URL
                    and 0 <= age.total_seconds() < _HISTORY_CACHE_SECONDS
                    and hashlib.sha256(raw).hexdigest() == saved["sha256"]
                ):
                    intervals = parse(raw)
                    record_cache_reuse(config, "sw", "industry_history")
                    return intervals
            except Exception as exc:
                logger.warning("Shenwan cached history invalid; refreshing: %s", exc)
        content = download()
        intervals = parse(content)
        try:
            write_json_atomic(
                path,
                {
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "url": SW_INDUSTRY_XLS_URL,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "content_b64": base64.b64encode(content).decode("ascii"),
                },
            )
        except OSError as exc:
            logger.warning("Shenwan valid history could not be cached: %s", exc)
        return intervals


def _intervals_from_sheet(pdf, pd) -> pl.DataFrame:
    if pdf.empty:
        raise RuntimeError("Shenwan industry XLS returned no rows")

    pdf = pdf.rename(
        columns={
            "股票代码": "code",
            "计入日期": "start_date",
            "行业代码": "industry_code",
            "更新日期": "update_time",
        }
    )
    pdf["start_date"] = pd.to_datetime(pdf["start_date"], errors="coerce").dt.date
    pdf = pdf.dropna(subset=["code", "start_date", "industry_code"])
    rows: list[dict] = []
    for rec in pdf.to_dict(orient="records"):
        sym = _code_to_symbol(str(rec["code"]))
        if not sym:
            continue
        rows.append(
            {
                "symbol": sym,
                "start_date": rec["start_date"],
                "industry_code": str(rec["industry_code"]).strip(),
            }
        )
    if not rows:
        raise RuntimeError("Shenwan industry XLS produced no all_a symbols")
    return (
        pl.DataFrame(rows)
        .unique(subset=["symbol", "start_date", "industry_code"], keep="last", maintain_order=True)
        .sort(["symbol", "start_date"])
    )


def expand_sw_industry_as_of(
    intervals: pl.DataFrame,
    as_of_dates: list[date],
) -> pl.DataFrame:
    """Point-in-time expand: for each as_of, latest start_date <= as_of per symbol."""
    if not as_of_dates:
        return pl.DataFrame(
            schema={
                "symbol": pl.Utf8,
                "classification_system": pl.Utf8,
                "industry_code": pl.Utf8,
                "industry_name": pl.Utf8,
                "as_of_date": pl.Date,
            }
        )
    frames: list[pl.DataFrame] = []
    for as_of in as_of_dates:
        snap = (
            intervals.filter(pl.col("start_date") <= as_of)
            .sort(["symbol", "start_date"])
            .unique(subset=["symbol"], keep="last")
            .select(
                pl.col("symbol"),
                pl.lit("sw").alias("classification_system"),
                pl.col("industry_code"),
                # Official name map is a separate SW publish; code is stable for joins.
                pl.col("industry_code").alias("industry_name"),
                pl.lit(as_of).alias("as_of_date"),
            )
        )
        if not snap.is_empty():
            frames.append(snap)
    if not frames:
        return pl.DataFrame(
            schema={
                "symbol": pl.Utf8,
                "classification_system": pl.Utf8,
                "industry_code": pl.Utf8,
                "industry_name": pl.Utf8,
                "as_of_date": pl.Date,
            }
        )
    return pl.concat(frames)
