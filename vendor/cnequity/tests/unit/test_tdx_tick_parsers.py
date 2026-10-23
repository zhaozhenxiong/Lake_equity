"""Byte-level tests for the two transaction parsers this project rewrote.

`get_transaction_data.py` and `get_history_transaction_data.py` are ports, not
copies: they fix three upstream defects and deliberately return prices raw. That
made them ours, but they carried no test — the only verification was a one-off
byte-for-byte comparison against a live TDX server during the mootdx migration,
which CI cannot repeat.

Transaction prices are delta-encoded *within a page*, so a single mis-read field
does not corrupt one row, it corrupts every row after it on that page. And
`trade_ticks` is a single-source dataset with no fallback to disagree with. The
tests below pin the wire layout the parsers assume: field order, the four filler
bytes only the historical response carries, the missing per-record trade count,
integer (not float) quantities, and undivided prices.

The encoder here is the inverse of `helper.get_price`, checked against it in
`test_price_encoder_round_trips_through_the_decoder` before anything else relies
on it.
"""

import struct

import pytest

from cnequity.adapters.tdx_protocol._wire.helper import get_price
from cnequity.adapters.tdx_protocol._wire.parser.std.get_history_transaction_data import (
    GetHistoryTransactionDataCmd,
)
from cnequity.adapters.tdx_protocol._wire.parser.std.get_transaction_data import (
    GetTransactionDataCmd,
)


def _encode_price(value: int) -> bytes:
    """Inverse of `helper.get_price`: sign in bit 6, continuation in bit 7."""
    magnitude = abs(value)
    first = (magnitude & 0x3F) | (0x40 if value < 0 else 0)
    magnitude >>= 6

    if not magnitude:
        return bytes([first])

    out = bytearray([first | 0x80])
    while True:
        chunk = magnitude & 0x7F
        magnitude >>= 7
        out.append(chunk | 0x80 if magnitude else chunk)
        if not magnitude:
            return bytes(out)


def _minutes(hour: int, minute: int) -> bytes:
    return struct.pack("<H", hour * 60 + minute)


def _same_session_record(hour, minute, price_diff, vol, trade_count, direction, reserved=0):
    return (
        _minutes(hour, minute)
        + _encode_price(price_diff)
        + _encode_price(vol)
        + _encode_price(trade_count)
        + _encode_price(direction)
        + _encode_price(reserved)
    )


def _history_record(hour, minute, price_diff, vol, direction, reserved=0):
    """No per-record trade count — the historical command simply does not send one."""
    return (
        _minutes(hour, minute)
        + _encode_price(price_diff)
        + _encode_price(vol)
        + _encode_price(direction)
        + _encode_price(reserved)
    )


def _same_session_body(*records: bytes) -> bytes:
    return struct.pack("<H", len(records)) + b"".join(records)


def _history_body(*records: bytes) -> bytes:
    # Four bytes between the count and the first record. Their content is not
    # read, so a non-zero filler is the honest fixture: a parser that forgot to
    # skip them would decode them as a time field and drift from here on.
    return struct.pack("<H", len(records)) + b"\xde\xad\xbe\xef" + b"".join(records)


def _parse_same_session(body: bytes):
    return GetTransactionDataCmd(None).parseResponse(body)


def _parse_history(body: bytes):
    return GetHistoryTransactionDataCmd(None).parseResponse(body)


@pytest.mark.parametrize("value", [0, 1, 63, 64, 100, 8191, 135060, 2_000_000, -1, -63, -4096])
def test_price_encoder_round_trips_through_the_decoder(value):
    """Everything below is built with `_encode_price`; pin it to the real decoder."""
    decoded, pos = get_price(_encode_price(value), 0)
    assert decoded == value
    assert pos == len(_encode_price(value))


def test_same_session_page_decodes_every_field_in_order():
    body = _same_session_body(
        _same_session_record(9, 30, price_diff=1_350_60, vol=12, trade_count=3, direction=0),
        _same_session_record(9, 31, price_diff=-40, vol=5, trade_count=1, direction=1),
    )

    rows = _parse_same_session(body)

    assert rows == [
        {
            "hour": 9,
            "minute": 30,
            "time": "09:30",
            "price_raw": 135060,
            "vol": 12,
            "trade_count": 3,
            "direction": 0,
        },
        {
            "hour": 9,
            "minute": 31,
            "time": "09:31",
            "price_raw": 135020,
            "vol": 5,
            "trade_count": 1,
            "direction": 1,
        },
    ]


def test_prices_are_returned_raw_and_undivided():
    """The regression that motivated the port.

    Upstream divides by 100 unconditionally, which is the A-share *stock*
    coefficient. A fund like 510300.SH is 0.001, so an unconditional /100 turned
    its turnover reconciliation into 10.004 and read 159915.SZ's 3.368 as 33.68.
    Scaling belongs to the caller, which knows the instrument; the parser must
    hand back the integer exactly as it came off the wire.
    """
    body = _same_session_body(
        _same_session_record(9, 30, price_diff=41_23, vol=1, trade_count=1, direction=0)
    )

    (row,) = _parse_same_session(body)

    assert row["price_raw"] == 4123
    assert isinstance(row["price_raw"], int)


def test_price_is_delta_accumulated_within_the_page():
    """Deltas are relative to zero at the start of *each* page, and may be negative."""
    body = _same_session_body(
        _same_session_record(9, 30, price_diff=1000, vol=1, trade_count=1, direction=0),
        _same_session_record(9, 31, price_diff=25, vol=1, trade_count=1, direction=0),
        _same_session_record(9, 32, price_diff=-125, vol=1, trade_count=1, direction=1),
    )

    assert [row["price_raw"] for row in _parse_same_session(body)] == [1000, 1025, 900]


def test_history_page_skips_the_four_filler_bytes():
    body = _history_body(
        _history_record(14, 56, price_diff=2000, vol=7, direction=0),
        _history_record(15, 0, price_diff=-10, vol=9, direction=1),
    )

    rows = _parse_history(body)

    assert [(row["time"], row["price_raw"], row["vol"]) for row in rows] == [
        ("14:56", 2000, 7),
        ("15:00", 1990, 9),
    ]


def test_history_rows_carry_no_trade_count():
    """Absent from the wire, so it must be absent from the row rather than faked."""
    body = _history_body(_history_record(9, 30, price_diff=100, vol=1, direction=0))

    (row,) = _parse_history(body)

    assert "trade_count" not in row
    assert list(row) == ["hour", "minute", "time", "price_raw", "vol", "direction"]


def test_quantities_stay_integers():
    """`vol` and `direction` come off `get_price`, the variable-length *integer*
    decoder — not `get_volume`, the custom float used for K-line volumes. Routing
    them through the float path would silently turn share counts into floats."""
    body = _same_session_body(
        _same_session_record(10, 15, price_diff=1, vol=59_700, trade_count=118, direction=1)
    )

    (row,) = _parse_same_session(body)

    assert (row["vol"], row["trade_count"], row["direction"]) == (59700, 118, 1)
    assert all(isinstance(row[key], int) for key in ("vol", "trade_count", "direction"))


@pytest.mark.parametrize(
    "parse,body", [(_parse_same_session, _same_session_body()), (_parse_history, _history_body())]
)
def test_empty_page_returns_no_rows(parse, body):
    """A short page is how both commands signal the end of a symbol-day."""
    assert parse(body) == []


def test_same_session_request_packet_layout():
    cmd = GetTransactionDataCmd(None)
    cmd.setParams(1, "600519", 0, 1800)

    assert bytes(cmd.send_pkg[:12]) == bytes.fromhex("0c1708010101 0e000e00 c50f".replace(" ", ""))
    assert struct.unpack("<H6sHH", cmd.send_pkg[12:]) == (1, b"600519", 0, 1800)


def test_history_request_packet_layout():
    cmd = GetHistoryTransactionDataCmd(None)
    cmd.setParams(1, "600519", 0, 2000, 20260731)

    assert bytes(cmd.send_pkg[:12]) == bytes.fromhex("0c0130010001 12001200 b50f".replace(" ", ""))
    assert struct.unpack("<IH6sHH", cmd.send_pkg[12:]) == (20260731, 1, b"600519", 0, 2000)


def test_an_already_encoded_code_is_packed_unchanged():
    """Callers pass str, but the encode step must be idempotent — `struct.pack`
    with `6s` would raise on a str, so a re-encode bug fails at the wire, not here."""
    from_str = GetTransactionDataCmd(None)
    from_str.setParams(1, "600519", 0, 1800)
    from_bytes = GetTransactionDataCmd(None)
    from_bytes.setParams(1, b"600519", 0, 1800)

    assert bytes(from_str.send_pkg) == bytes(from_bytes.send_pkg)

    history = GetHistoryTransactionDataCmd(None)
    history.setParams(1, b"600519", 0, 2000, 20260731)

    assert struct.unpack("<IH6sHH", history.send_pkg[12:]) == (20260731, 1, b"600519", 0, 2000)


@pytest.mark.parametrize("bad_date", [0, 19891231, 21000101, 20260731000, "not-a-date"])
def test_history_request_rejects_a_date_that_is_not_yyyymmdd(bad_date):
    """Upstream's guard was `if type(date) is (type(date) is str) or ...` — a
    comparison against a bool, so always False. Every malformed date sailed
    through and got packed as whatever `struct` made of it."""
    with pytest.raises(ValueError):
        GetHistoryTransactionDataCmd(None).setParams(1, "600519", 0, 2000, bad_date)
