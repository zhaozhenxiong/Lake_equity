"""Per-product contract specifications for futures and options.

The daily files say which contracts traded and at what price. They do not say
how many units one lot is, or whether an option can be exercised early. Those
come from each product's contract rules, which change rarely but do change, so
some products carry more than one version. A version takes effect by the
contract's **delivery month** (``from_delivery``), because that is how the
exchanges roll a revision in: the new terms apply from a named contract
onward, while contracts already listed keep the old ones.

Multipliers were measured rather than transcribed. SHFE publishes per-product
turnover, volume and average price in every file since 2002 (and CZCE, GFEX
and later SHFE files publish per-contract turnover), so ``turnover /
(volume × price)`` recovers the lot size. It was sampled once a year across
each exchange's whole history (2026-09-25) and snapped to the nearest standard
size. It was constant for every product except three:

- **SHFE fu**: 10 t → 50 t from ~fu1208, then back to 10 t in the 2018 relaunch
  (fu1901 onward). The product-level ratio moved 10.0 → 10.4 → 11.6 → 39.7
  → 50.0 between 2011-08 and 2011-12 as the new contracts took volume.
- **SHFE ru**: 5 t → 10 t from RU1208 (the exchange's own 2012 review names
  RU1208 as the first 10-ton contract).
- **SHFE pb**: 25 t → 5 t on 2013-09 for the whole product at once. The ratio
  jumped 25 → 5 between 2013-08-27 and 2013-09-06 with no mixed session, so
  contracts that were live across that week changed size mid-life. The table
  can only give one size per contract; it gives pb1309 onward 5 t.

Option lots are one underlying futures contract, so an option's multiplier is
its underlying's (the CZCE reference file says so for every product:
「1手…期货合约」). CFFEX index options are the exception, at 100 CNY a point.

Exercise styles: every commodity option is American, except SHFE copper and
gold, which were European until the series on CU2211 and AU2212 (SHFE,
2020-07-31 notice). CFFEX index options are European.

Ticks are each product's current minimum price increment, taken from the
traded prices of the latest sampled session. The audit checks that traded
prices sit on this grid, so a wrong entry surfaces there. DCE option ticks are
the only entries not read off prices: DCE options are not collected while its
own endpoints are unreachable, and the values are there for the opt-in
official route.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

Kind = Literal["future", "option"]
ExerciseStyle = Literal["american", "european"]

__all__ = ["PRODUCTS", "ProductSpec", "product_spec"]


@dataclass(frozen=True)
class ProductSpec:
    exchange: str
    product: str
    kind: Kind
    name: str
    multiplier: float
    tick_size: float
    quote_unit: str
    exercise_style: ExerciseStyle | None = None
    #: First delivery month this version governs; ``None`` means from the start.
    from_delivery: date | None = None


def _f(exchange, product, name, multiplier, tick, unit, from_delivery=None):
    return ProductSpec(
        exchange, product, "future", name, multiplier, tick, unit, None, from_delivery
    )


_T = "元/吨"
_FUTURES: tuple[ProductSpec, ...] = (
    # SHFE
    _f("SHF", "CU", "铜", 5, 10, _T),
    _f("SHF", "AL", "铝", 5, 5, _T),
    _f("SHF", "ZN", "锌", 5, 5, _T),
    _f("SHF", "PB", "铅", 25, 5, _T),
    _f("SHF", "PB", "铅", 5, 5, _T, date(2013, 9, 1)),
    _f("SHF", "NI", "镍", 1, 10, _T),
    _f("SHF", "SN", "锡", 1, 10, _T),
    _f("SHF", "AO", "氧化铝", 20, 1, _T),
    _f("SHF", "AD", "铸造铝合金", 10, 5, _T),
    _f("SHF", "AU", "黄金", 1000, 0.02, "元/克"),
    _f("SHF", "AG", "白银", 15, 1, "元/千克"),
    _f("SHF", "RB", "螺纹钢", 10, 1, _T),
    _f("SHF", "WR", "线材", 10, 1, _T),
    _f("SHF", "HC", "热轧卷板", 10, 1, _T),
    _f("SHF", "SS", "不锈钢", 5, 5, _T),
    _f("SHF", "FU", "燃料油", 10, 1, _T),
    _f("SHF", "FU", "燃料油", 50, 1, _T, date(2012, 8, 1)),
    _f("SHF", "FU", "燃料油", 10, 1, _T, date(2019, 1, 1)),
    _f("SHF", "BU", "石油沥青", 10, 1, _T),
    _f("SHF", "RU", "天然橡胶", 5, 5, _T),
    _f("SHF", "RU", "天然橡胶", 10, 5, _T, date(2012, 8, 1)),
    _f("SHF", "BR", "丁二烯橡胶", 5, 5, _T),
    _f("SHF", "SP", "纸浆", 10, 2, _T),
    _f("SHF", "OP", "胶版印刷纸", 40, 2, _T),
    # INE (2018 futures have a separate official daily file)
    _f("INE", "SC", "原油", 1000, 0.1, "元/桶"),
    _f("INE", "LU", "低硫燃料油", 10, 1, _T),
    _f("INE", "NR", "20号胶", 10, 5, _T),
    _f("INE", "BC", "国际铜", 5, 10, _T),
    _f("INE", "EC", "集运指数(欧线)", 50, 0.1, "点"),
    # CZCE, including products renamed away (ER→RI, ME→MA, RO→OI, TC→ZC,
    # WS→WH), which keep their own codes in the files that carried them.
    _f("CZC", "AP", "苹果", 10, 1, _T),
    _f("CZC", "CF", "棉花", 5, 5, _T),
    _f("CZC", "CJ", "红枣", 5, 5, _T),
    _f("CZC", "CY", "棉纱", 5, 5, _T),
    _f("CZC", "ER", "早籼稻(旧)", 10, 1, _T),
    _f("CZC", "FG", "玻璃", 20, 1, _T),
    _f("CZC", "JR", "粳稻", 20, 1, _T),
    _f("CZC", "LR", "晚籼稻", 20, 1, _T),
    _f("CZC", "MA", "甲醇", 10, 1, _T),
    _f("CZC", "ME", "甲醇(旧)", 50, 1, _T),
    _f("CZC", "OI", "菜籽油", 10, 1, _T),
    _f("CZC", "PF", "短纤", 5, 2, _T),
    _f("CZC", "PK", "花生", 5, 2, _T),
    _f("CZC", "PL", "丙烯", 20, 1, _T),
    _f("CZC", "PM", "普麦", 50, 1, _T),
    _f("CZC", "PR", "瓶片", 15, 2, _T),
    _f("CZC", "PX", "对二甲苯", 5, 2, _T),
    _f("CZC", "RI", "早籼稻", 20, 1, _T),
    _f("CZC", "RM", "菜籽粕", 10, 1, _T),
    _f("CZC", "RO", "菜籽油(旧)", 5, 2, _T),
    _f("CZC", "RS", "油菜籽", 10, 1, _T),
    _f("CZC", "SA", "纯碱", 20, 1, _T),
    _f("CZC", "SF", "硅铁", 5, 2, _T),
    _f("CZC", "SH", "烧碱", 30, 1, _T),
    _f("CZC", "SM", "锰硅", 5, 2, _T),
    _f("CZC", "SR", "白糖", 10, 1, _T),
    _f("CZC", "TA", "PTA", 5, 2, _T),
    _f("CZC", "TC", "动力煤(旧)", 200, 0.2, _T),
    _f("CZC", "UR", "尿素", 20, 1, _T),
    _f("CZC", "WH", "强麦", 20, 1, _T),
    _f("CZC", "WS", "强麦(旧)", 10, 1, _T),
    _f("CZC", "WT", "硬麦", 10, 1, _T),
    _f("CZC", "ZC", "动力煤", 100, 0.2, _T),
    # DCE. Not measured: no source reachable from here publishes DCE turnover,
    # so these lots are the contract rules' (DCE's own site is behind a
    # challenge). Ticks were read off Sina's traded prices, 2026-09-25. FB was
    # re-denominated from 500 sheets to 10 m3 in 2019-12 for every live
    # contract at once; FB2001 onward carries the new terms here, which is
    # wrong for those contracts' sessions before the switch.
    _f("DCE", "A", "黄大豆1号", 10, 1, _T),
    _f("DCE", "B", "黄大豆2号", 10, 1, _T),
    _f("DCE", "M", "豆粕", 10, 1, _T),
    _f("DCE", "Y", "豆油", 10, 1, _T),
    _f("DCE", "P", "棕榈油", 10, 1, _T),
    _f("DCE", "C", "玉米", 10, 1, _T),
    _f("DCE", "CS", "玉米淀粉", 10, 1, _T),
    _f("DCE", "JD", "鸡蛋", 10, 1, "元/500千克"),
    _f("DCE", "L", "聚乙烯", 5, 1, _T),
    _f("DCE", "V", "聚氯乙烯", 5, 1, _T),
    _f("DCE", "PP", "聚丙烯", 5, 1, _T),
    _f("DCE", "EG", "乙二醇", 10, 1, _T),
    _f("DCE", "EB", "苯乙烯", 5, 1, _T),
    _f("DCE", "PG", "液化石油气", 20, 1, _T),
    _f("DCE", "I", "铁矿石", 100, 0.5, _T),
    _f("DCE", "J", "焦炭", 100, 0.5, _T),
    _f("DCE", "JM", "焦煤", 60, 0.5, _T),
    _f("DCE", "RR", "粳米", 10, 1, _T),
    _f("DCE", "LH", "生猪", 16, 5, _T),
    _f("DCE", "FB", "纤维板", 500, 0.05, "元/张"),
    _f("DCE", "FB", "纤维板", 10, 0.5, "元/立方米", date(2020, 1, 1)),
    _f("DCE", "BB", "胶合板", 500, 0.05, "元/张"),
    _f("DCE", "LG", "原木", 90, 0.5, "元/立方米"),
    _f("DCE", "BZ", "纯苯", 30, 1, _T),
    # GFEX
    _f("GFE", "SI", "工业硅", 5, 5, _T),
    _f("GFE", "LC", "碳酸锂", 1, 20, _T),
    _f("GFE", "PS", "多晶硅", 3, 5, _T),
    _f("GFE", "PT", "铂", 1000, 0.05, "元/克"),
    _f("GFE", "PD", "钯", 1000, 0.05, "元/克"),
    # CFFEX: index futures at 300/200 CNY a point; treasury futures quoted per
    # 100 CNY of face, so 1,000,000 face moves 10,000 CNY a point (20,000 for
    # the 2,000,000-face two-year).
    _f("CFE", "IF", "沪深300股指期货", 300, 0.2, "点"),
    _f("CFE", "IH", "上证50股指期货", 300, 0.2, "点"),
    _f("CFE", "IC", "中证500股指期货", 200, 0.2, "点"),
    _f("CFE", "IM", "中证1000股指期货", 200, 0.2, "点"),
    _f("CFE", "TS", "2年期国债期货", 20000, 0.002, "元(百元面值)"),
    _f("CFE", "TF", "5年期国债期货", 10000, 0.005, "元(百元面值)"),
    _f("CFE", "T", "10年期国债期货", 10000, 0.005, "元(百元面值)"),
    _f("CFE", "TL", "30年期国债期货", 10000, 0.01, "元(百元面值)"),
)

# (exchange, product, name, tick) of every option product; the lot follows
# the underlying future.
_OPTION_TICKS: tuple[tuple[str, str, str, float], ...] = (
    ("SHF", "CU", "铜期权", 2),
    ("SHF", "AL", "铝期权", 1),
    ("SHF", "ZN", "锌期权", 1),
    ("SHF", "PB", "铅期权", 1),
    ("SHF", "NI", "镍期权", 2),
    ("SHF", "SN", "锡期权", 2),
    ("SHF", "AO", "氧化铝期权", 0.5),
    ("SHF", "AD", "铸造铝合金期权", 1),
    ("SHF", "AU", "黄金期权", 0.02),
    ("SHF", "AG", "白银期权", 0.5),
    ("SHF", "RB", "螺纹钢期权", 0.5),
    ("SHF", "HC", "热轧卷板期权", 0.5),
    ("SHF", "SS", "不锈钢期权", 1),
    ("SHF", "FU", "燃料油期权", 0.5),
    ("SHF", "BU", "石油沥青期权", 0.5),
    ("SHF", "RU", "天然橡胶期权", 1),
    ("SHF", "BR", "丁二烯橡胶期权", 1),
    ("SHF", "SP", "纸浆期权", 1),
    ("SHF", "OP", "胶版印刷纸期权", 1),
    ("INE", "SC", "原油期权", 0.05),
    ("INE", "BC", "国际铜期权", 2),
    ("INE", "NR", "20号胶期权", 1),
    ("INE", "LU", "低硫燃料油期权", 0.5),
    ("DCE", "M", "豆粕期权", 0.5),
    ("DCE", "C", "玉米期权", 0.5),
    ("DCE", "I", "铁矿石期权", 0.1),
    ("DCE", "PG", "液化石油气期权", 0.2),
    ("DCE", "L", "聚乙烯期权", 0.5),
    ("DCE", "V", "聚氯乙烯期权", 0.5),
    ("DCE", "PP", "聚丙烯期权", 0.5),
    ("DCE", "P", "棕榈油期权", 0.5),
    ("DCE", "A", "黄大豆1号期权", 0.5),
    ("DCE", "B", "黄大豆2号期权", 0.5),
    ("DCE", "Y", "豆油期权", 0.5),
    ("DCE", "EG", "乙二醇期权", 0.5),
    ("DCE", "EB", "苯乙烯期权", 0.5),
    ("DCE", "JD", "鸡蛋期权", 0.5),
    ("DCE", "CS", "玉米淀粉期权", 0.5),
    ("DCE", "LH", "生猪期权", 5),
    ("DCE", "LG", "原木期权", 0.5),
    ("CZC", "AP", "苹果期权", 0.5),
    ("CZC", "CF", "棉花期权", 1),
    ("CZC", "CJ", "红枣期权", 1),
    ("CZC", "FG", "玻璃期权", 0.5),
    ("CZC", "MA", "甲醇期权", 0.5),
    ("CZC", "OI", "菜籽油期权", 0.5),
    ("CZC", "PF", "短纤期权", 0.5),
    ("CZC", "PK", "花生期权", 0.5),
    ("CZC", "PL", "丙烯期权", 0.5),
    ("CZC", "PR", "瓶片期权", 0.5),
    ("CZC", "PX", "对二甲苯期权", 0.5),
    ("CZC", "RM", "菜籽粕期权", 0.5),
    ("CZC", "SA", "纯碱期权", 0.5),
    ("CZC", "SF", "硅铁期权", 1),
    ("CZC", "SH", "烧碱期权", 0.5),
    ("CZC", "SM", "锰硅期权", 1),
    ("CZC", "SR", "白糖期权", 0.5),
    ("CZC", "TA", "PTA期权", 0.5),
    ("CZC", "UR", "尿素期权", 0.5),
    ("CZC", "ZC", "动力煤期权", 0.1),
    ("GFE", "SI", "工业硅期权", 1),
    ("GFE", "LC", "碳酸锂期权", 10),
    ("GFE", "PS", "多晶硅期权", 1),
    ("GFE", "PT", "铂期权", 0.05),
    ("GFE", "PD", "钯期权", 0.05),
)

#: Series from which SHFE copper and gold options became American.
_AMERICAN_FROM = {("SHF", "CU"): date(2022, 11, 1), ("SHF", "AU"): date(2022, 12, 1)}


def _options() -> tuple[ProductSpec, ...]:
    by_future: dict[tuple[str, str], list[ProductSpec]] = {}
    for spec in _FUTURES:
        by_future.setdefault((spec.exchange, spec.product), []).append(spec)
    out: list[ProductSpec] = []
    for exchange, product, name, tick in _OPTION_TICKS:
        switch = _AMERICAN_FROM.get((exchange, product))
        for future in by_future[(exchange, product)]:
            base = dict(
                exchange=exchange,
                product=product,
                kind="option",
                name=name,
                multiplier=future.multiplier,
                tick_size=tick,
                quote_unit=future.quote_unit,
            )
            if switch is None:
                out.append(
                    ProductSpec(
                        **base, exercise_style="american", from_delivery=future.from_delivery
                    )
                )
            else:
                out.append(
                    ProductSpec(
                        **base, exercise_style="european", from_delivery=future.from_delivery
                    )
                )
                out.append(ProductSpec(**base, exercise_style="american", from_delivery=switch))
    for product, name in (
        ("IO", "沪深300股指期权"),
        ("MO", "中证1000股指期权"),
        ("HO", "上证50股指期权"),
    ):
        out.append(ProductSpec("CFE", product, "option", name, 100, 0.2, "点", "european"))
    return tuple(out)


PRODUCTS: tuple[ProductSpec, ...] = _FUTURES + _options()

_BY_KEY: dict[tuple[str, str, str], list[ProductSpec]] = {}
for _spec in PRODUCTS:
    _BY_KEY.setdefault((_spec.exchange, _spec.product, _spec.kind), []).append(_spec)
for _versions in _BY_KEY.values():
    _versions.sort(key=lambda s: s.from_delivery or date.min)


def product_spec(
    exchange: str,
    product: str,
    kind: Kind,
    on: date | None = None,
    *,
    delivery: date | None = None,
) -> ProductSpec | None:
    """The spec governing a contract of *product* delivering in *delivery*.

    ``on`` is accepted for callers that only know an observation date; a
    contract never delivers before it trades, so it is a safe lower bound.
    Unknown is a real answer: an exchange lists new products, and a contract
    of one this table has not met still gets its bars — with a null
    multiplier and an audit finding, not a guessed one.
    """
    # FB changed units for already-live contracts. A single contract-level
    # spec cannot describe the transition; do not apply the new multiplier
    # retrospectively until dated authoritative terms are available.
    if (
        exchange == "DCE"
        and product == "FB"
        and on is not None
        and on < date(2020, 1, 1)
        and delivery is not None
        and delivery >= date(2020, 1, 1)
    ):
        return None
    versions = _BY_KEY.get((exchange, product, kind))
    if not versions:
        return None
    month = delivery or on
    current = versions[0]
    for spec in versions:
        if spec.from_delivery is None or (month is not None and spec.from_delivery <= month):
            current = spec
    return current
