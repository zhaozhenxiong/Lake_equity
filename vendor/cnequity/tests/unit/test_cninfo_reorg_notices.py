from datetime import date

from cnequity.adapters.cninfo.reorg_notices import combine_notices, parse_reorg_notice

# Phrasing from real issuer notices (600306.SH 2023, 300506.SZ 2025, 600734.SH 2022).
TIP = (
    "以现有总股本231,574,918股为基数，按照每10股转增8.5股的比例实施资本公积金转增股本。"
    "本次资本公积金转增股本后，公司股票除权除息参考价格为8.14元/股。"
    "本次资本公积转增股本股权登记日为2023年12月25日，除权除息日为2023年12月26日。"
)
IMPLEMENTATION = "按每10股转增10.5股的比例实施资本公积转增股本，共计转增730,000,000股。"
OPENING = (
    "关于资本公积金转增股本实施后首个交易日开盘参考价调整的提示公告。"
    "股权登记日次一交易日（2025年12月22日）的股票开盘参考价将进行调整，"
    "按照公司调整后的除权参考价格的计算公式进行除权调整为3.16元/股。"
)
ESTIMATE = (
    "按每10股转增25股的比例实施。除权除息日为2022年2月15日。"
    "本次资本公积金转增股本后，公司股票除权参考价格将根据未来公司股票价格的持续波动情况进行调整。"
    "若按2022年2月7日收盘价3.17元/股计算，除权参考价格为1.32元/股。"
)


def test_one_notice_states_the_whole_event():
    event = combine_notices([parse_reorg_notice(TIP)])
    assert event == {
        "transfer_ratio": 0.85,
        "ex_date": date(2023, 12, 26),
        "reference_prices": [8.14],
    }


def test_facts_split_across_two_notices_combine():
    event = combine_notices([parse_reorg_notice(IMPLEMENTATION), parse_reorg_notice(OPENING)])
    assert event == {
        "transfer_ratio": 1.05,
        "ex_date": date(2025, 12, 22),
        "reference_prices": [3.16],
    }


def test_a_hypothetical_close_is_not_a_reference_price():
    # "若按…收盘价3.17元/股" follows a sentence break; only 1.32 is a stated price.
    assert parse_reorg_notice(ESTIMATE)["reference_prices"] == [1.32]


def test_disagreeing_notices_give_no_event():
    other = TIP.replace("每10股转增8.5股", "每10股转增9股")
    assert combine_notices([parse_reorg_notice(TIP), parse_reorg_notice(other)]) is None
    assert parse_reorg_notice("2024年年度权益分派实施公告：每10股派1元。") is None


def test_a_price_stated_after_the_adjustment_clause_is_read():
    # 000796.SZ 2023: the price follows a comma inside the same clause, and the
    # average conversion price quoted before it is not a reference price.
    text = (
        "关于重整计划资本公积金转增股本除权事项进展暨公司股票复牌的公告。"
        "该收盘价高于转增股本的平均价格3.77元/股，公司股权登记日次一交易日（2023年12月20日）"
        "的股票开盘参考价应当依据公司本次调整后的除权参考价格的计算公式进行除权调整，调整为3.93元/股。"
    )
    assert parse_reorg_notice(text)["reference_prices"] == [3.93]


def test_an_approximate_ratio_is_read():
    text = "以股本667,843,529股为基数，按每10股转增约9.71919股的比例实施资本公积金转增股本。"
    assert parse_reorg_notice(text)["transfer_ratios"] == [0.971919]
