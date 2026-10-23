from datetime import date

import polars as pl

from cnequity.domain.action_sessions import effective_session


def test_halted_ex_date_maps_to_first_security_session(halted_ex_event):
    halt = halted_ex_event("510300.SH", date(2025, 1, 7), date(2025, 1, 8), date(2025, 1, 9))
    result = effective_session(halt["actions"], halt["sessions"])
    assert result.select("ex_date", "effective_session").to_dicts() == [
        {"ex_date": halt["ex_date"], "effective_session": halt["resumed"]}
    ]


def test_an_action_after_last_known_trade_has_no_effective_session(halted_ex_event):
    halt = halted_ex_event("510300.SH", date(2025, 1, 7), date(2025, 1, 8), date(2025, 1, 9))
    actions = pl.DataFrame({"symbol": [halt["symbol"]], "ex_date": [date(2025, 1, 10)]})
    assert effective_session(actions, halt["sessions"])["effective_session"].to_list() == [None]
