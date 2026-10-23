"""Offline tests for `TdxWireClient` — the trimmed client, which is ours.

`_wire/__init__.py` is the one file in the vendored tree that never existed in
tdxpy: the seven-call surface, the page caps, and the heartbeat choice are all
decisions made here. It went unmeasured for as long as the whole tree was
treated as upstream code.

The caps are the part worth pinning. TDX does not reject an oversized request,
it silently returns a short page, so a cap that stopped being applied would not
raise anywhere — it would just quietly cost rows on every deep backfill.

Nothing here opens a socket: the command classes are replaced with recorders.
"""

import pytest

from cnequity.adapters.tdx_protocol import _wire
from cnequity.adapters.tdx_protocol._wire import (
    MARKET_SZ,
    MAX_PAGE,
    MAX_TICK_PAGE,
    TdxWireClient,
)
from cnequity.adapters.tdx_protocol._wire.constants import TDXParams


@pytest.fixture
def recorded(monkeypatch):
    """Swap every command class for a recorder; yield the call log."""
    log: list[tuple[str, tuple]] = []

    def recorder(name):
        class _Recorder:
            def __init__(self, client, lock=None):
                self.client = client
                self.lock = lock

            def setParams(self, *args):  # noqa: N802 - upstream shape
                self.args = args

            def call_api(self):
                log.append((name, self.args))
                return [{"cmd": name}]

        return _Recorder

    for attr in (
        "GetSecurityBarsCmd",
        "GetIndexBarsCmd",
        "GetSecurityCountCmd",
        "GetSecurityList",
        "GetTransactionDataCmd",
        "GetHistoryTransactionDataCmd",
        "GetXdXrInfo",
    ):
        monkeypatch.setattr(_wire, attr, recorder(attr))

    return log


def test_bar_pages_are_capped_at_the_protocol_limit(recorded):
    """TDX truncates above 800 rather than erroring; ask for more and you lose them."""
    TdxWireClient().get_security_bars(9, 1, "600519", 0, 5000)
    TdxWireClient().get_index_bars(9, 1, "000001", 800, 5000)

    assert recorded == [
        ("GetSecurityBarsCmd", (9, 1, "600519", 0, MAX_PAGE)),
        ("GetIndexBarsCmd", (9, 1, "000001", 800, MAX_PAGE)),
    ]


def test_transaction_pages_use_the_deeper_tick_cap(recorded):
    """Ticks page deeper than bars — the two caps are not the same number."""
    TdxWireClient().get_transaction_data(1, "600519", 0, 9999)
    TdxWireClient().get_history_transaction_data(1, "600519", 2000, 9999, 20260731)

    assert recorded == [
        ("GetTransactionDataCmd", (1, "600519", 0, MAX_TICK_PAGE)),
        ("GetHistoryTransactionDataCmd", (1, "600519", 2000, MAX_TICK_PAGE, 20260731)),
    ]
    assert MAX_TICK_PAGE > MAX_PAGE


def test_a_request_under_the_cap_is_passed_through_untouched(recorded):
    """The cap is a ceiling, not a page size: daily increments ask for far less."""
    TdxWireClient().get_security_bars(9, 0, "000001", 0, 3)

    assert recorded == [("GetSecurityBarsCmd", (9, 0, "000001", 0, 3))]


def test_the_caps_match_the_protocol_constants():
    assert MAX_PAGE == 800
    assert MAX_TICK_PAGE == TDXParams.MAX_TRANSACTION_COUNT


def test_counts_are_coerced_so_a_string_page_size_cannot_reach_the_wire(recorded):
    """`min("5000", 800)` would compare str to int and raise inside the socket
    layer, where the failure reads as a connection problem."""
    TdxWireClient().get_security_bars(9, 1, "600519", 0, "5000")

    assert recorded == [("GetSecurityBarsCmd", (9, 1, "600519", 0, MAX_PAGE))]


def test_the_unpaged_calls_pass_their_parameters_through(recorded):
    """Neither takes a page size: the security list pages by `start` alone, and
    xdxr returns a symbol's whole corporate-action history in one response."""
    TdxWireClient().get_security_list(1, 1000)
    TdxWireClient().get_xdxr_info(1, "600519")

    assert recorded == [
        ("GetSecurityList", (1, 1000)),
        ("GetXdXrInfo", (1, "600519")),
    ]


def test_heartbeat_asks_shenzhen_for_a_security_count(recorded):
    """The cheapest round trip the protocol has, and the callback the vendored
    `HeartBeatThread` invokes by name. Upstream passed `secrets.randbelow(1)`,
    which is always 0 — the randomness was decorative."""
    TdxWireClient().do_heartbeat()

    assert recorded == [("GetSecurityCountCmd", (MARKET_SZ,))]


def test_setup_runs_the_three_handshake_commands_in_order(monkeypatch):
    order: list[str] = []

    def recorder(name):
        class _Recorder:
            def __init__(self, client):
                self.client = client

            def call_api(self):
                order.append(name)

        return _Recorder

    for attr in ("SetupCmd1", "SetupCmd2", "SetupCmd3"):
        monkeypatch.setattr(_wire, attr, recorder(attr))

    TdxWireClient().setup()

    assert order == ["SetupCmd1", "SetupCmd2", "SetupCmd3"]


def test_to_df_refuses_rather_than_quietly_building_a_frame():
    """Upstream returned pandas here. Dropping pandas is why the tree is vendored;
    a silent no-op would let a caller think it had a frame."""
    with pytest.raises(NotImplementedError):
        TdxWireClient().to_df([{"close": 1}])
