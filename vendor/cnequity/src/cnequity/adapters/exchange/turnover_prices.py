"""Historical exchange price evidence, never spliced exchange-wide turnover."""

from datetime import date

import httpx
import polars as pl

from cnequity.adapters.exchange.daily_quotes import SZSE_URL
from cnequity.adapters.exchange.etf_profiles import _get_official_response, _workbook_records
from cnequity.adapters.exchange.fund_volume import PARAMS, URL_ROOT, parse_sse_dayk
from cnequity.storage.raw_archive import RawPayloadArchive

COLUMNS = ["symbol", "trade_date", "open", "high", "low", "close"]
SCHEMA = {"symbol": pl.String, "trade_date": pl.Date, **dict.fromkeys(COLUMNS[2:], pl.Float64)}


def parse_szse_history_prices(payload: bytes, day: date, symbols: set[str]) -> pl.DataFrame:
    rows = _workbook_records(payload)
    required = {"交易日期", "证券代码", "开盘", "最高", "最低", "今收"}
    if not rows or not required <= set(rows[0]):
        raise ValueError("SZSE historical price report missing columns")
    result, seen = [], set()
    for row in rows:
        if str(row.get("交易日期")) != str(day):
            raise ValueError("SZSE report has no evidence for the requested historical date")
        code = str(row.get("证券代码", "")).zfill(6)
        if len(code) != 6 or not code.isdigit() or code in seen:
            raise ValueError("SZSE price report has invalid/duplicate securities")
        seen.add(code)
        symbol = code + ".SZ"
        if symbol not in symbols:
            continue
        try:
            values = [float(str(row[k]).replace(",", "")) for k in ["开盘", "最高", "最低", "今收"]]
        except (TypeError, ValueError):
            continue
        result.append(dict(zip(COLUMNS, [symbol, day, *values], strict=True)))
    frame = pl.DataFrame(result, schema=SCHEMA)
    return frame.filter(
        pl.all_horizontal(pl.col(c).is_finite() & (pl.col(c) > 0) for c in COLUMNS[2:])
    )


def fetch_exchange_turnover_prices(targets, *, config, run_id):
    if targets.is_empty() or not config.sources.get("exchange", True):
        return pl.DataFrame(schema=SCHEMA), []
    archive = RawPayloadArchive(config.meta_root, enabled=True, datasets=["daily_bars"])
    requests = []
    for symbol in targets.filter(pl.col("symbol").str.ends_with(".SH"))["symbol"].unique().sort():
        days = set(targets.filter(pl.col("symbol") == symbol)["trade_date"])
        requests.append((symbol, URL_ROOT + symbol.split(".")[0], PARAMS, days, "json"))
    for day in targets.filter(pl.col("symbol").str.ends_with(".SZ"))["trade_date"].unique().sort():
        symbols = set(targets.filter(pl.col("trade_date") == day)["symbol"])
        requests.append((str(day), SZSE_URL.format(day=day), {}, symbols, "xlsx"))
    frames, failures = [], []
    with httpx.Client(timeout=45, follow_redirects=True) as client:
        for scope, url, params, wanted, kind in requests:
            try:
                response = _get_official_response(
                    config,
                    client,
                    url,
                    params=params,
                    referer="https://www.sse.com.cn/" if kind == "json" else "https://www.szse.cn/",
                )
                archive.archive(
                    "daily_bars",
                    response.content,
                    source="exchange",
                    run_id=run_id,
                    url=url,
                    request_params=params,
                    payload_format=kind,
                )
                frame = (
                    parse_sse_dayk(response.content, scope, trade_dates=wanted).select(COLUMNS)
                    if kind == "json"
                    else parse_szse_history_prices(
                        response.content, date.fromisoformat(scope), wanted
                    )
                )
                frames.append(frame)
            except (ValueError, RuntimeError, httpx.HTTPError) as exc:
                failures.append({"scope": scope, "error": str(exc)})
    return (pl.concat(frames) if frames else pl.DataFrame(schema=SCHEMA)), failures
