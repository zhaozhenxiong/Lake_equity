"""Quotes facade over the vendored TDX wire client.

Keeps the call shape the lake already used (``bars`` / ``index`` / ``xdxr`` /
``stocks`` returning row dicts) so the migration off mootdx touches this module
and ``client.py`` only — ``bars.py`` and ``corporate_actions.py`` are unchanged.

What mootdx contributed on top of the wire protocol was small and is
reimplemented here: derive the market, cap the page at 800, page the security
list, and alias ``vol`` to ``volume``. Everything else it added (tenacity
retries, a tqdm progress bar on stderr, pandas conversion) the lake either does
itself or does not want.
"""

from __future__ import annotations

from datetime import date

from cnequity.adapters.tdx_protocol._wire import (
    CATEGORY_DAILY,
    MAX_PAGE,
    MAX_TICK_PAGE,
    TdxWireClient,
)

# TDX market ids: 0 = Shenzhen, 1 = Shanghai.
MARKET_SZ = 0
MARKET_SH = 1

# Prefix rules lifted from mootdx's get_stock_market, kept so a caller that does
# not pass an explicit market keeps its previous behaviour.
_SH_PREFIXES = ("50", "51", "60", "68", "90", "110", "113", "132", "204")

# Indices resolve differently from stocks: mootdx routes 00/88/99 to Shanghai,
# which is why 000001 means the SH composite here but Ping An as a stock code.
_SH_INDEX_PREFIXES = ("00", "88", "99")

_SECURITY_LIST_PAGE = 1000


def market_for_stock(symbol: str) -> int:
    return MARKET_SH if symbol.startswith(_SH_PREFIXES) else MARKET_SZ


def market_for_index(symbol: str) -> int:
    return MARKET_SH if symbol[:2] in _SH_INDEX_PREFIXES else MARKET_SZ


def _with_volume(rows: list[dict] | None) -> list[dict]:
    """Alias ``vol`` to ``volume``; downstream schemas read the latter."""
    if not rows:
        return []
    for row in rows:
        if "vol" in row and "volume" not in row:
            row["volume"] = row["vol"]
    return rows


class Quotes:
    """Thin, connection-owning wrapper. Construct via :func:`factory`."""

    def __init__(self, client: TdxWireClient, server: tuple[str, int]):
        self._client = client
        self.server = server

    # --- lifecycle ---------------------------------------------------------

    @classmethod
    def factory(
        cls,
        *,
        server: tuple[str, int],
        timeout: int = 10,
        multithread: bool = False,
        heartbeat: bool = False,
    ) -> Quotes:
        host, port = server
        client = TdxWireClient(multithread=multithread, heartbeat=heartbeat)
        client.connect(host, int(port), time_out=timeout)
        return cls(client, (host, int(port)))

    def security_quotes(self, pairs: list[tuple[int, str]]) -> list[dict]:
        """Batch quotes, up to 80 (market, code) pairs per call; see ``quote_bars``."""
        return self._client.get_security_quotes(pairs)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 — close must never mask the real error
            pass

    @property
    def last_response_wire(self) -> bytes | None:
        """Exact bytes received by the most recent protocol request.

        ``TdxWireClient`` parsers operate on its socket object, so the
        response capture is exposed through that object rather than the
        normalized row-returning facade.  Test doubles may provide the same
        property directly on themselves.
        """
        wire = getattr(self._client, "last_response_wire", None)
        if wire is None:
            wire = getattr(getattr(self._client, "client", None), "last_response_wire", None)
        return wire

    def __enter__(self) -> Quotes:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # --- data --------------------------------------------------------------

    def bars(
        self,
        symbol: str,
        frequency: int = CATEGORY_DAILY,
        market: int | None = None,
        start: int = 0,
        offset: int = MAX_PAGE,
    ) -> list[dict]:
        """Daily bars for a stock.

        Unlike mootdx — whose ``bars()`` had no ``market`` parameter and silently
        dropped one passed as a kwarg — an explicit market is honoured here. The
        caller derives it from the exchange suffix, which is authoritative;
        the prefix heuristic is only a fallback.
        """
        mkt = market_for_stock(symbol) if market is None else int(market)
        rows = self._client.get_security_bars(
            int(frequency), mkt, str(symbol), int(start), min(int(offset), MAX_PAGE)
        )
        return _with_volume(rows)

    def index(
        self,
        symbol: str,
        frequency: int = CATEGORY_DAILY,
        start: int = 0,
        offset: int = MAX_PAGE,
    ) -> list[dict]:
        """Index bars — must not go through :meth:`bars`, which mis-decodes them."""
        rows = self._client.get_index_bars(
            int(frequency),
            market_for_index(symbol),
            str(symbol),
            int(start),
            min(int(offset), MAX_PAGE),
        )
        return _with_volume(rows)

    def ticks(
        self,
        symbol: str,
        market: int | None = None,
        start: int = 0,
        offset: int = MAX_TICK_PAGE,
    ) -> list[dict]:
        """Same-session transaction records. ``start=0`` is the newest block."""
        mkt = market_for_stock(symbol) if market is None else int(market)
        rows = self._client.get_transaction_data(
            mkt, str(symbol), int(start), min(int(offset), MAX_TICK_PAGE)
        )
        return _with_volume(rows or [])

    def ticks_history(
        self,
        symbol: str,
        on_date: date | int,
        market: int | None = None,
        start: int = 0,
        offset: int = MAX_TICK_PAGE,
    ) -> list[dict]:
        """Transaction records for a past session.

        ``price_raw`` is left as the protocol's integer: the 1/100 scale that
        turns it into yuan holds for A-share stocks and not for funds or bonds
        (``_wire.constants.SECURITY_COEFFICIENT``), and this facade does not
        know which it was handed.
        """
        mkt = market_for_stock(symbol) if market is None else int(market)
        stamp = int(on_date.strftime("%Y%m%d")) if isinstance(on_date, date) else int(on_date)
        rows = self._client.get_history_transaction_data(
            mkt, str(symbol), int(start), min(int(offset), MAX_TICK_PAGE), stamp
        )
        return _with_volume(rows or [])

    def xdxr(self, symbol: str, market: int | None = None) -> list[dict]:
        mkt = market_for_stock(symbol) if market is None else int(market)
        return self._client.get_xdxr_info(mkt, str(symbol)) or []

    def stocks(self, market: int) -> list[dict]:
        """Full security list for one market, paged."""
        if market not in (MARKET_SZ, MARKET_SH):
            raise ValueError(f"unsupported TDX market id: {market!r}")
        count = self._client.get_security_count(market) or 0
        out: list[dict] = []
        for start in range(0, int(count), _SECURITY_LIST_PAGE):
            page = self._client.get_security_list(market, start)
            if not page:
                raise RuntimeError(
                    f"TDX security list for market={market} ended at offset={start} "
                    f"before declared count={count}"
                )
            out.extend(page)
        return out


def quote_bars(client, symbols: list[str], trade_date: date, *, pace=None) -> dict[str, dict]:
    """``daily_bars`` rows for *trade_date* from TDX batch quotes, 80 per request.

    Only for the session the quotes describe (see
    ``domain.market_time.last_closed_session``; the caller checks). A symbol
    that did not trade (volume 0), lacks a price coefficient, or does not come
    back is simply absent: the per-symbol path owns it.

    Measured 2026-09-26 against the 2026-09-24 TDX daily bars, 5,222 SH/SZ
    symbols in 66 requests (11.8 s): OHLC and amount identical for every traded
    symbol; volume identical for 99.3%, and one lot lower for 38 names — the
    quote truncates odd lots where the K-line rounds them.
    """
    from cnequity.adapters.tdx_protocol._wire import MAX_QUOTES_PER_REQUEST
    from cnequity.adapters.tdx_protocol._wire.helper import get_security_coefficient

    pairs = []
    for symbol in symbols:
        code, _, exchange = symbol.partition(".")
        if exchange in ("SH", "SZ"):
            pairs.append((MARKET_SH if exchange == "SH" else MARKET_SZ, code))
    out: dict[str, dict] = {}
    fetch = getattr(client, "security_quotes", None) or client.get_security_quotes
    for offset in range(0, len(pairs), MAX_QUOTES_PER_REQUEST):
        if pace is not None:
            pace()
        for row in fetch(pairs[offset : offset + MAX_QUOTES_PER_REQUEST]) or []:
            if int(row.get("vol") or 0) <= 0:
                continue
            try:
                coefficient = get_security_coefficient(row["market"], row["code"])
            except NotImplementedError:
                continue
            prices = [
                round(row[key] * coefficient, 4)
                for key in ("open_raw", "high_raw", "low_raw", "price_raw")
            ]
            if min(prices) <= 0:
                continue
            symbol = f"{row['code']}.{'SH' if row['market'] == MARKET_SH else 'SZ'}"
            last_close = row.get("last_close_raw")
            pre_close = round(last_close * coefficient, 4) if last_close else None
            out[symbol] = {
                "symbol": symbol,
                "trade_date": trade_date,
                "open": prices[0],
                "high": prices[1],
                "low": prices[2],
                "close": prices[3],
                "pre_close": pre_close if pre_close and pre_close > 0 else None,
                "volume": int(row["vol"]) * 100,
                "amount": float(row["amount"]),
            }
    return out
