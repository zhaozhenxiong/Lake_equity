"""Dated publisher membership/weight workbooks; weights are ratios, not percent."""

import io
import math
from datetime import date

import pandas as pd
import polars as pl

from cnequity.domain.symbols import is_all_a_symbol

INDEX_COUNTS = {"000300.SH": 300, "000688.SH": 50, "399001.SZ": 500, "399006.SZ": 100}
CNI_WEIGHT_URL = "https://www.cnindex.com.cn/sample-detail/download-history"
CSI_ROOT = "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile"


def snapshot_requests(index_symbol: str):
    if index_symbol not in INDEX_COUNTS:
        raise ValueError(f"unsupported official index snapshot: {index_symbol}")
    code = index_symbol.split(".")[0]
    if index_symbol.endswith(".SZ"):
        return [(CNI_WEIGHT_URL, {"indexcode": code}, True, "cni")]
    return [
        (f"{CSI_ROOT}/{kind}/{code}{kind}.xls", {}, kind == "closeweight", "exchange")
        for kind in ("cons", "closeweight")
    ]


def parse_index_snapshot(payload: bytes, index_symbol: str, *, weights: bool) -> pl.DataFrame:
    if index_symbol not in INDEX_COUNTS:
        raise ValueError("unsupported official index snapshot")
    sheet = pd.read_excel(io.BytesIO(payload))
    if index_symbol.endswith(".SZ"):
        columns = ("日期", "样本代码", None, None, "权重（%）")
    else:
        columns = (
            "日期Date",
            "成份券代码Constituent Code",
            "交易所Exchange",
            "指数代码 Index Code",
            "权重(%)weight",
        )
    day_col, code_col, exchange_col, index_col, weight_col = columns
    required = {c for c in columns[:-1] if c}
    if weights:
        required.add(weight_col)
    if not required <= set(sheet.columns) or len(sheet) != INDEX_COUNTS[index_symbol]:
        raise ValueError("official index workbook has missing columns or incomplete membership")
    rows = []
    for record in sheet.to_dict(orient="records"):
        raw_day = str(record[day_col])
        day = date.fromisoformat(
            raw_day if "-" in raw_day else f"{raw_day[:4]}-{raw_day[4:6]}-{raw_day[6:8]}"
        )
        code = str(record[code_col]).zfill(6)
        if index_col and str(record[index_col]).zfill(6) != index_symbol.split(".")[0]:
            raise ValueError("official workbook identifies a different index")
        exchange = (
            {"上海证券交易所": "SH", "深圳证券交易所": "SZ"}.get(record.get(exchange_col))
            if exchange_col
            else "SZ"
        )
        if exchange is None or not is_all_a_symbol(code, exchange):
            raise ValueError("official workbook has an unsupported constituent")
        weight = float(record[weight_col]) / 100 if weights else None
        if weights and (not math.isfinite(weight) or not 0 <= weight <= 1):
            raise ValueError("official workbook weight is outside 0..100 percent")
        rows.append(
            {
                "index_symbol": index_symbol,
                "symbol": f"{code}.{exchange}",
                "as_of_date": day,
                "weight": weight,
            }
        )
    frame = pl.DataFrame(
        rows,
        schema={
            "index_symbol": pl.Utf8,
            "symbol": pl.Utf8,
            "as_of_date": pl.Date,
            "weight": pl.Float64,
        },
    )
    if frame["as_of_date"].n_unique() != 1 or frame["symbol"].n_unique() != frame.height:
        raise ValueError("official workbook mixes dates or duplicates constituents")
    if weights and not 0.99 <= frame["weight"].sum() <= 1.01:
        raise ValueError("official workbook weights do not sum to 100 percent")
    return frame
