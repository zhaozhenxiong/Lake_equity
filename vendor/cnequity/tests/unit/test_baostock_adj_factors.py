"""Offline tests for Baostock's own adjust-factor table."""

from datetime import date

from cnequity.adapters.baostock.adj_factors import fetch_adjust_factor_events_baostock_many

_FIELDS = ["code", "dividOperateDate", "foreAdjustFactor", "backAdjustFactor", "adjustFactor"]


class _Result:
    def __init__(self, rows, *, error_code="0"):
        self.error_code = error_code
        self.error_msg = "" if error_code == "0" else "query failed"
        self.fields = _FIELDS
        self._rows = rows
        self._index = -1

    def next(self):
        self._index += 1
        return self._index < len(self._rows)

    def get_row_data(self):
        return self._rows[self._index]


class _Baostock:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def login(self):
        return _Result([])

    def query_adjust_factor(self, code, start_date, end_date):
        self.calls.append(code)
        return _Result(self.rows.get(code, []))

    def logout(self):
        return None


def test_one_query_per_symbol_returns_back_factors_by_ex_date():
    bs = _Baostock(
        {
            "sh.600519": [
                ["sh.600519", "2001-08-27", "0.1", "1.000000", "1.000000"],
                ["sh.600519", "2025-12-19", "0.9", "7.491968", "7.491968"],
                ["sh.600519", "2026-06-26", "1.0", "", ""],
            ],
            "sz.000001": [],
        }
    )
    frame, failed = fetch_adjust_factor_events_baostock_many(
        ["600519.SH", "000001.SZ", "830799.BJ"],
        date(1990, 1, 1),
        date(2026, 9, 28),
        bs=bs,
    )
    assert bs.calls == ["sh.600519", "sz.000001"]
    assert frame.rows() == [
        ("600519.SH", date(2001, 8, 27), 1.0),
        ("600519.SH", date(2025, 12, 19), 7.491968),
    ]
    # An empty table and an unserved exchange both leave no baostock opinion.
    assert failed == ["000001.SZ", "830799.BJ"]
