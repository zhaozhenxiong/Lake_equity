"""Isolated change-table and full-cohort reconstruction contracts."""

from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest

from cnequity.adapters.cni import index_announcements as notices
from cnequity.adapters.cni.index_announcements import (
    MembershipChange,
    parse_announcement,
    parse_delisting_date,
    reconstruct_membership,
)

PERIODIC = """
决定于2024年12月16日对指数实施调整。
深证成份指数样本股调整名单
调入名单 调出名单 证券代码 证券简称 证券代码 证券简称
000099 中信海直 000402 金融街
指数公告 Announcement 2 / 2
301536 星宸科技 301239 普瑞眼科
创业板指数样本股调整名单
调入名单 调出名单 证券代码 证券简称 证券代码 证券简称
300024 机器人 300296 利亚德
深证100指数样本股调整名单
调入名单 调出名单 证券代码 证券简称 证券代码 证券简称
000975 山金国际 002271 东方雨虹
"""
TEMPORARY = """
决定于2025年2月17日进行临时调整。
指数代码 指数简称 调入股票 调出股票
证券代码 证券简称 证券代码 证券简称
399001 深证成指 302132 中航成飞 300114 中航电测
399006 创业板指 302132 中航成飞 300114 中航电测
399007 深证300 302132 中航成飞 300114 中航电测
"""


def html(text):
    return ("<html><script>决定于1999年1月1日</script><p>" + text + "</p></html>").encode()


@pytest.mark.parametrize(
    "symbol,expected", [("399001.SZ", {"000099.SZ", "301536.SZ"}), ("399006.SZ", {"300024.SZ"})]
)
def test_periodic_scopes_and_page_break(symbol, expected):
    change = parse_announcement(html(PERIODIC), kind="html", index_symbol=symbol)
    assert change.effective_date == date(2024, 12, 16)
    assert change.additions == expected


@pytest.mark.parametrize("symbol", ["399001.SZ", "399006.SZ"])
def test_temporary_one_pair_per_index(symbol):
    change = parse_announcement(html(TEMPORARY), kind="html", index_symbol=symbol)
    assert change.additions == {"302132.SZ"}
    assert change.removals == {"300114.SZ"}


def test_pdf_number_boundaries(monkeypatch):
    monkeypatch.setattr(
        notices,
        "PdfReader",
        lambda _: SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: PERIODIC)]),
    )
    change = parse_announcement(b"isolated PDF input", kind="pdf", index_symbol="399001.SZ")
    assert change.additions == {"000099.SZ", "301536.SZ"}


@pytest.mark.parametrize(
    "text",
    [
        PERIODIC.replace("调入名单 调出名单", "调出名单 调入名单"),
        PERIODIC.replace("301536 星宸科技", "000099 星宸科技"),
        PERIODIC.replace("301239", "无代码"),
        PERIODIC.replace("000099", "200099"),
        PERIODIC.replace("000099", "000402"),
        PERIODIC.replace("决定于2024年12月16日", "将于2024年12月16日"),
        PERIODIC + "决定于2025年1月1日",
        TEMPORARY.replace("调入股票 调出股票", "调出股票 调入股票"),
        TEMPORARY.replace("300114 中航电测", "无代码 中航电测"),
        TEMPORARY.replace("399001 深证成指", "1399001 深证成指"),
    ],
)
def test_reject_ambiguous_or_malformed_tables(text):
    with pytest.raises(ValueError):
        parse_announcement(html(text), kind="html", index_symbol="399001.SZ")


def test_conditional_date_requires_identified_issuer(monkeypatch):
    text = "证券代码：002013 股票将于2023年3月17日起终止上市并摘牌。"
    monkeypatch.setattr(
        notices,
        "PdfReader",
        lambda _: SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: text)]),
    )
    conditional = (
        TEMPORARY.replace("决定于2025年2月17日", "决定于中航机电退市日起")
        .replace("300114", "002013")
        .replace("302132", "000422")
    )
    with pytest.raises(ValueError):
        parse_announcement(html(conditional), kind="html", index_symbol="399001.SZ")
    change = parse_announcement(
        html(conditional), kind="html", index_symbol="399001.SZ", delisting_authority=b"issuer PDF"
    )
    assert change.effective_date == date(2023, 3, 17)
    with pytest.raises(ValueError, match="issuer identity"):
        parse_delisting_date(b"issuer PDF", code="300114")
    with pytest.raises(ValueError, match="removed member"):
        parse_announcement(
            html(conditional.replace("002013", "002014")),
            kind="html",
            index_symbol="399001.SZ",
            delisting_authority=b"issuer PDF",
        )
    text += "股票将于2023年3月18日起终止上市。"
    with pytest.raises(ValueError, match="unique"):
        parse_delisting_date(b"issuer PDF", code="002013")


def inputs():
    opening = frozenset(f"{code:06d}.SZ" for code in range(1, 101))
    change = MembershipChange(
        "399006.SZ", date(2020, 6, 15), frozenset({"000101.SZ"}), frozenset({"000001.SZ"})
    )
    return dict(
        index_symbol="399006.SZ",
        opening_date=date(2020, 1, 1),
        opening_members=opening,
        closing_date=date(2020, 9, 30),
        closing_members=(opening - change.removals) | change.additions,
        changes=[change],
    )


def test_whole_books_on_actual_effective_dates_with_null_weights():
    frame = reconstruct_membership(**inputs())
    assert frame.height == 200
    assert frame["weight"].null_count() == 200
    assert frame.group_by("as_of_date").len()["len"].to_list() == [100, 100]
    assert set(frame["as_of_date"]) == {date(2020, 1, 1), date(2020, 6, 15)}


@pytest.mark.parametrize(
    "defect",
    [
        "wrong_closing",
        "duplicate_date",
        "wrong_index",
        "out_of_window",
        "nonmember",
        "existing",
        "unequal",
        "bad_symbol",
        "wrong_size",
        "invalid_window",
    ],
)
def test_no_publication_on_anchor_or_transition_disagreement(defect):
    kwargs = inputs()
    change = kwargs["changes"][0]
    if defect == "wrong_closing":
        kwargs["closing_members"] = kwargs["opening_members"]
    elif defect == "duplicate_date":
        kwargs["changes"].append(change)
    elif defect == "wrong_index":
        kwargs["changes"] = [replace(change, index_symbol="399001.SZ")]
    elif defect == "out_of_window":
        kwargs["changes"] = [replace(change, effective_date=date(2019, 1, 1))]
    elif defect == "nonmember":
        kwargs["changes"] = [replace(change, removals=frozenset({"000105.SZ"}))]
    elif defect == "existing":
        kwargs["changes"] = [replace(change, additions=frozenset({"000002.SZ"}))]
    elif defect == "unequal":
        kwargs["changes"] = [replace(change, additions=frozenset({"000101.SZ", "000102.SZ"}))]
    elif defect == "bad_symbol":
        kwargs["opening_members"] = (kwargs["opening_members"] - {"000001.SZ"}) | {"000001bad.SZ"}
    elif defect == "wrong_size":
        kwargs["opening_members"] -= {"000001.SZ"}
    else:
        kwargs["closing_date"] = kwargs["opening_date"]
    with pytest.raises(ValueError):
        reconstruct_membership(**kwargs)


def test_multiple_temporary_rows_are_all_parsed():
    text = TEMPORARY.replace(
        "399006 创业板指 302132 中航成飞 300114 中航电测",
        "399001 深证成指 000422 湖北宜化 002013 中航机电",
    )
    change = parse_announcement(html(text), kind="html", index_symbol="399001.SZ")
    assert change.additions == {"302132.SZ", "000422.SZ"}
    assert change.removals == {"300114.SZ", "002013.SZ"}


def workbook(rows):
    from io import BytesIO

    import pandas as pd

    buffer = BytesIO()
    pd.DataFrame(rows).to_excel(buffer, index=False, engine="openpyxl")
    return buffer.getvalue()


def workbook_rows():
    return [
        {
            "指数代码": "399001",
            "指数简称": "深证成指",
            "证券代码": "000099",
            "证券简称": "中信海直",
            "调整类型": "+",
        },
        {
            "指数代码": "399001",
            "指数简称": "深证成指",
            "证券代码": "000402",
            "证券简称": "金融街",
            "调整类型": "-",
        },
        {
            "指数代码": "399006",
            "指数简称": "创业板指",
            "证券代码": "300024",
            "证券简称": "机器人",
            "调整类型": "+",
        },
        {
            "指数代码": "399006",
            "指数简称": "创业板指",
            "证券代码": "300296",
            "证券简称": "利亚德",
            "调整类型": "-",
        },
    ]


def test_legacy_workbook_keeps_codes_directions_and_index_scope():
    from cnequity.adapters.cni.index_announcements import parse_change_workbook

    change = parse_change_workbook(
        workbook(workbook_rows()), index_symbol="399001.SZ", effective_date=date(2024, 12, 16)
    )
    assert change.additions == {"000099.SZ"}
    assert change.removals == {"000402.SZ"}
    assert change.effective_date == date(2024, 12, 16)


@pytest.mark.parametrize(
    "defect",
    [
        "duplicate",
        "identity",
        "unknown_type",
        "overlap",
        "unequal",
        "wrong_code",
        "columns",
        "missing_index",
    ],
)
def test_legacy_workbook_rejects_unsupported_rows(defect):
    from cnequity.adapters.cni.index_announcements import parse_change_workbook

    rows = workbook_rows()
    if defect == "duplicate":
        rows.append(rows[0])
    elif defect == "identity":
        rows[0]["指数简称"] = "其它指数"
    elif defect == "unknown_type":
        rows[0]["调整类型"] = "OLD"
    elif defect == "overlap":
        rows[0]["证券代码"] = rows[1]["证券代码"]
    elif defect == "unequal":
        rows.append({**rows[0], "证券代码": "000100"})
    elif defect == "wrong_code":
        rows[0]["证券代码"] = "600099"
    elif defect == "columns":
        for row in rows:
            del row["调整类型"]
    else:
        rows = rows[2:]
    with pytest.raises(ValueError):
        parse_change_workbook(
            workbook(rows), index_symbol="399001.SZ", effective_date=date(2024, 12, 16)
        )


def legacy_pdf(monkeypatch, text):
    monkeypatch.setattr(
        notices,
        "PdfReader",
        lambda _: SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: text)]),
    )


def test_reviewed_digest_pins_broken_text_and_multiple_rows(monkeypatch):
    import hashlib

    from cnequity.adapters.cni.index_announcements import parse_reviewed_legacy_table

    payload = b"exact reviewed original"
    legacy_pdf(monkeypatch, "399001 000560 002477\n399001 002203 002143\n399002 R 000560 002477")
    kwargs = dict(
        index_symbol="399001.SZ",
        effective_date=date(2019, 5, 13),
        reviewed_sha256=hashlib.sha256(payload).hexdigest(),
    )
    change = parse_reviewed_legacy_table(payload, **kwargs)
    assert change.additions == {"000560.SZ", "002203.SZ"}
    assert change.removals == {"002477.SZ", "002143.SZ"}
    with pytest.raises(ValueError, match="reviewed original"):
        parse_reviewed_legacy_table(payload + b"changed", **kwargs)
    legacy_pdf(monkeypatch, "399001 000560 002477 002143\n399002 R 000560 002477")
    with pytest.raises(ValueError, match="explicit"):
        parse_reviewed_legacy_table(payload, **kwargs)
    legacy_pdf(monkeypatch, "399001 000560 002477\n399001 000560 002143\n399002 R 000560 002477")
    with pytest.raises(ValueError, match="duplicate"):
        parse_reviewed_legacy_table(payload, **kwargs)


MERGER = "鉴于招商地产(000024)即将被招商蛇口(001979)吸收合并而退市，决定对深证成份指数进行调整，具体为：于2015年12月30日调入招商蛇口，调出招商地产。"


def test_named_merger_has_codes_direction_scope_and_effective_date(monkeypatch):
    from cnequity.adapters.cni.index_announcements import parse_named_merger

    legacy_pdf(monkeypatch, MERGER)
    change = parse_named_merger(b"issuer identity and change text", index_symbol="399001.SZ")
    assert change.effective_date == date(2015, 12, 30)
    assert change.additions == {"001979.SZ"}
    assert change.removals == {"000024.SZ"}


@pytest.mark.parametrize(
    "text",
    [
        MERGER.replace("(000024)", ""),
        MERGER.replace("调入招商蛇口，调出招商地产", "调入招商地产，调出招商蛇口"),
        MERGER.replace("深证成份指数", "深证100指数"),
        MERGER.replace("001979", "000024"),
        MERGER + "于2016年1月1日调入招商蛇口，调出招商地产。",
    ],
)
def test_named_merger_rejects_unproven_identity_or_direction(monkeypatch, text):
    from cnequity.adapters.cni.index_announcements import parse_named_merger

    legacy_pdf(monkeypatch, text)
    with pytest.raises(ValueError):
        parse_named_merger(b"notice", index_symbol="399001.SZ")
