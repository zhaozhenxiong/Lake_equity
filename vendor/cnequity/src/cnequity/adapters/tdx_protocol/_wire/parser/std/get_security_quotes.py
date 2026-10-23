# cython: language_level=3
"""Batch real-time quotes: up to 80 securities per request (TDX 0x053e).

The layout is the standard-market quotes packet as published by pytdx/tdxpy
(MIT). Prices are returned as the raw wire integers — the scale depends on
the security type (``SECURITY_COEFFICIENT``), and ``quotes.py`` applies it —
so this parser never guesses a decimal point.
"""

import struct
from collections import OrderedDict

from cnequity.adapters.tdx_protocol._wire.helper import get_price, get_volume
from cnequity.adapters.tdx_protocol._wire.parser.base import BaseParser

MAX_QUOTES_PER_REQUEST = 80


class GetSecurityQuotesCmd(BaseParser):
    def setParams(self, all_stock):
        """:param all_stock: [(market, code), ...], at most 80."""
        count = len(all_stock)
        if count <= 0 or count > MAX_QUOTES_PER_REQUEST:
            raise ValueError(f"quotes need 1..{MAX_QUOTES_PER_REQUEST} securities, got {count}")
        body_len = count * 7 + 12
        header = struct.pack(
            "<HIHHIIHH", 0x10C, 0x02006320, body_len, body_len, 0x5053E, 0, 0, count
        )
        pkg = bytearray(header)
        for market, code in all_stock:
            if isinstance(code, str):
                code = code.encode("utf-8")
            pkg.extend(struct.pack("<B6s", market, code))
        self.send_pkg = pkg

    def parseResponse(self, body_buf):
        pos = 2  # b1 cb
        (count,) = struct.unpack("<H", body_buf[pos : pos + 2])
        pos += 2
        quotes = []
        for _ in range(count):
            market, code, active1 = struct.unpack("<B6sH", body_buf[pos : pos + 9])
            pos += 9
            price, pos = get_price(body_buf, pos)
            last_close_diff, pos = get_price(body_buf, pos)
            open_diff, pos = get_price(body_buf, pos)
            high_diff, pos = get_price(body_buf, pos)
            low_diff, pos = get_price(body_buf, pos)
            server_time, pos = get_price(body_buf, pos)
            _neg_price, pos = get_price(body_buf, pos)
            vol, pos = get_price(body_buf, pos)
            cur_vol, pos = get_price(body_buf, pos)
            (amount_raw,) = struct.unpack("<I", body_buf[pos : pos + 4])
            pos += 4
            s_vol, pos = get_price(body_buf, pos)
            b_vol, pos = get_price(body_buf, pos)
            _r2, pos = get_price(body_buf, pos)
            _r3, pos = get_price(body_buf, pos)
            for _level in range(5):  # bid, ask, bid_vol, ask_vol per level
                for _ in range(4):
                    _value, pos = get_price(body_buf, pos)
            pos += 2  # reversed_bytes4
            for _ in range(4):  # reversed_bytes5..8
                _value, pos = get_price(body_buf, pos)
            _r9, active2 = struct.unpack("<hH", body_buf[pos : pos + 4])
            pos += 4
            quotes.append(
                OrderedDict(
                    [
                        ("market", market),
                        ("code", code.decode("utf-8")),
                        ("active1", active1),
                        ("active2", active2),
                        # Raw wire integers; scale by the security coefficient.
                        ("price_raw", price),
                        ("last_close_raw", price + last_close_diff),
                        ("open_raw", price + open_diff),
                        ("high_raw", price + high_diff),
                        ("low_raw", price + low_diff),
                        ("server_time", server_time),
                        ("vol", vol),
                        ("cur_vol", cur_vol),
                        ("amount", get_volume(amount_raw)),
                        ("s_vol", s_vol),
                        ("b_vol", b_vol),
                    ]
                )
            )
        return quotes
