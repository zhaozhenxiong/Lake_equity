"""SHFE/INE annual workbook reader, verified on the 2026 combined archive.

The official download label is not a kind discriminator: a workbook can hold
both futures and options. This reader never downloads, extracts files or
publishes data. Callers retain archive hashes and reconcile overlapping daily
keys before using it. Missing daily-only fields remain null. Verified
2009–2019 grouped XLS files require their two-sided-count note and are
converted to the one-sided schema. Other layouts require separate verification.
"""

from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile

import openpyxl
import polars as pl
import xlrd

from cnequity.adapters.futures_exchange.common import (
    FuturesPayloadError,
    parse_number,
    parse_option_settle,
    parse_price,
)
from cnequity.adapters.futures_exchange.shfe import INE_PRODUCTS
from cnequity.domain.derivatives import parse_future_code, parse_option_code
from cnequity.domain.schemas import derivative_bar_violations, validate_dataframe, with_provenance

HEADER = (
    "合约",
    "交易日期",
    "前收盘",
    "前结算",
    "开盘价",
    "最高价",
    "最低价",
    "收盘价",
    "结算价",
    "涨跌1",
    "涨跌2",
    "成交量",
    "成交金额(万元)",
    "持仓量",
)

VERIFIED_PRE_2020_YEARS = frozenset({2002, 2003, 2004, 2005, 2006, 2007, 2008})
GROUPED_XLS_HEADER = (
    "合约",
    "日期",
    "前收盘",
    "前结算",
    "开盘价",
    "最高价",
    "最低价",
    "收盘价",
    "结算价",
    "涨跌1",
    "涨跌2",
    "成交量",
    "成交金额",
    "持仓量",
)


def _count(value) -> int:
    number = parse_number(value)
    if number is None or number < 0 or not number.is_integer():
        raise FuturesPayloadError(f"annual workbook: invalid count {value!r}")
    return int(number)


def _convert_row(raw, year: int, *, count_divisor: int = 1) -> tuple[str, dict] | None:
    if all(value in (None, "") for value in raw):
        return None
    code = str(raw[0] or "").strip()
    try:
        day = datetime.strptime(str(raw[1]), "%Y%m%d").date()
        if day.year != year:
            raise ValueError("unexpected year")
        product = code[: next(i for i, c in enumerate(code) if c.isdigit())]
        exchange = "INE" if product.lower() in INE_PRODUCTS else "SHF"
        try:
            contract = parse_future_code(code, exchange, day)
            dataset = "futures_bars"
        except ValueError:
            contract = parse_option_code(code, exchange, day)
            dataset = "option_bars"
    except (ValueError, StopIteration) as exc:
        raise FuturesPayloadError(f"annual workbook: invalid contract/date {code!r}") from exc
    raw_volume = _count(raw[11])
    raw_interest = _count(raw[13])
    if raw_volume % count_divisor or raw_interest % count_divisor:
        raise FuturesPayloadError("annual workbook: non-divisible two-sided count")
    volume = raw_volume // count_divisor
    amount = parse_number(raw[12])
    row = {
        "symbol": contract.symbol,
        "exchange": exchange,
        "exchange_code": code,
        "product": contract.product,
        "trade_date": day,
        **{
            name: parse_price(raw[index]) if volume else None
            for name, index in [("open", 4), ("high", 5), ("low", 6), ("close", 7)]
        },
        "settle": parse_option_settle(raw[8]) if dataset == "option_bars" else parse_price(raw[8]),
        "pre_settle": parse_price(raw[3]),
        "volume": volume,
        "amount": round(amount * 10000 / count_divisor, 2) if amount is not None else None,
        "open_interest": raw_interest // count_divisor,
        "oi_change": None,
    }
    if (
        dataset == "futures_bars"
        and row["settle"] is None
        and volume == 0
        and row["open_interest"] == 0
    ):
        # Early files list unopened far months absent from the daily report.
        return None
    if dataset == "option_bars":
        row.update(
            underlying_symbol=contract.underlying_symbol,
            option_type=contract.option_type,
            strike=contract.strike,
            exercise_volume=None,
            delta=None,
            implied_vol=None,
            series_implied_vol=None,
        )
    return dataset, row


def _frames(
    raw_rows, year: int, rejected: list[dict] | None = None, *, count_divisor: int = 1
) -> dict[str, pl.DataFrame]:
    rows: dict[str, list[dict]] = {"futures_bars": [], "option_bars": []}
    for raw in raw_rows:
        converted = _convert_row(raw, year, count_divisor=count_divisor)
        if converted is not None:
            dataset, row = converted
            rows[dataset].append(row)
    result = {}
    for dataset, data in rows.items():
        if not data:
            continue
        frame = with_provenance(
            pl.DataFrame(data, infer_schema_length=None),
            source="futures_exchange",
            data_version="shfe_archive_v1",
        )
        if dataset == "option_bars":
            frame = frame.with_columns(
                pl.col("delta", "implied_vol", "series_implied_vol").cast(pl.Float64),
                pl.col("exercise_volume").cast(pl.Int64),
            )
        invalid = derivative_bar_violations(dataset)
        bad = frame.filter(invalid)
        if not bad.is_empty():
            if rejected is None:
                raise FuturesPayloadError(f"annual workbook: {bad.height} invalid {dataset} row(s)")
            rejected.extend({"dataset": dataset, **row} for row in bad.to_dicts())
            frame = frame.filter(~invalid.fill_null(False))
        frame = validate_dataframe(frame, dataset)
        if frame.height != frame.unique(subset=["symbol", "trade_date"]).height:
            raise FuturesPayloadError("annual workbook: duplicate contract/date")
        result[dataset] = frame
    return result


def parse_workbook(
    body: bytes, *, year: int, rejected: list[dict] | None = None
) -> dict[str, pl.DataFrame]:
    """Parse a single verified-layout XLSX, rejecting wrong dates and duplicates."""
    if year < 2020 and year not in VERIFIED_PRE_2020_YEARS:
        raise FuturesPayloadError("annual workbook: pre-2020 counting basis is unverified")
    book = openpyxl.load_workbook(io.BytesIO(body), read_only=True, data_only=True)
    try:

        def raw_rows():
            for sheet in book:
                header = next(sheet.iter_rows(min_row=3, max_row=3, values_only=True), ())
                if tuple(header[:14]) != HEADER or any(
                    value not in (None, "") for value in header[14:]
                ):
                    raise FuturesPayloadError(
                        f"annual workbook: unverified header in {sheet.title}"
                    )
                yield from sheet.iter_rows(min_row=5, values_only=True)

        return _frames(raw_rows(), year, rejected)
    finally:
        book.close()


def parse_grouped_xls(
    body: bytes, *, year: int, rejected: list[dict] | None = None
) -> dict[str, pl.DataFrame]:
    """Read verified SHFE BIFF workbook; grouped contract codes are forward-filled."""
    if year not in {
        2009,
        2010,
        2011,
        2012,
        2013,
        2014,
        2015,
        2016,
        2017,
        2018,
        2019,
        2020,
        2021,
        2022,
        2023,
        2024,
    }:
        raise FuturesPayloadError("annual XLS: layout unverified for this year")
    book = xlrd.open_workbook(file_contents=body, on_demand=True)
    try:

        def raw_rows():
            for sheet in book.sheets():
                header = sheet.row_values(2)
                shifted = year == 2021 and tuple(header[:15]) == ("品种", *GROUPED_XLS_HEADER)
                if not shifted and tuple(header[:14]) != GROUPED_XLS_HEADER:
                    raise FuturesPayloadError(f"annual XLS: unverified header in {sheet.name}")
                if year in {2009, 2010, 2011, 2012, 2013, 2014, 2015, 2016, 2017, 2018, 2019}:
                    footer = " ".join(
                        str(value)
                        for index in range(max(0, sheet.nrows - 6), sheet.nrows)
                        for value in sheet.row_values(index)
                    )
                    if "成交量、持仓量" not in footer or "双边计算" not in footer:
                        raise FuturesPayloadError("annual XLS: two-sided note missing")
                code = None
                day_cell = str(sheet.row_values(3)[2 if shifted else 1])
                first_row = 3 if len(day_cell) == 8 and day_cell.isdigit() else 4
                for index in range(first_row, sheet.nrows):
                    raw = sheet.row_values(index)[1 if shifted else 0 :]
                    if not raw[1]:
                        # The official file ends data with a blank separator,
                        # followed by explanatory notes.
                        if any(value not in (None, "") for value in raw):
                            raise FuturesPayloadError("annual XLS: unexpected undated row")
                        break
                    if raw[0]:
                        code = str(raw[0]).strip()
                    if not code:
                        raise FuturesPayloadError("annual XLS: missing grouped contract code")
                    raw[0] = code
                    yield raw

        return _frames(
            raw_rows(),
            year,
            rejected,
            count_divisor=2
            if year in {2009, 2010, 2011, 2012, 2013, 2014, 2015, 2016, 2017, 2018, 2019}
            else 1,
        )
    finally:
        book.release_resources()


def iter_archive(path: Path, *, year: int, rejected: list[dict] | None = None):
    """Yield (member name, dataset frames) without extracting archive paths."""
    with ZipFile(path) as archive:
        members = [member for member in archive.infolist() if not member.is_dir()]
        if not members or len(members) > 100:
            raise FuturesPayloadError("annual archive: invalid member count")
        if sum(member.file_size for member in members) > 1_000_000_000:
            raise FuturesPayloadError("annual archive: uncompressed size exceeds limit")
        for member in members:
            ext = member.filename.lower()
            if not ext.endswith((".xlsx", ".xls")) or member.file_size > 200_000_000:
                raise FuturesPayloadError(
                    f"annual archive member {member.filename}: unverified type or size"
                )
            try:
                body = archive.read(member)
                if ext.endswith(".xls"):
                    frames = parse_grouped_xls(body, year=year, rejected=rejected)
                else:
                    frames = parse_workbook(body, year=year, rejected=rejected)
            except Exception as exc:
                raise FuturesPayloadError(
                    f"annual archive member {member.filename}: {type(exc).__name__}: {exc}"
                ) from exc
            yield member.filename, frames
