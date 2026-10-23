"""Published BaoStock access rules: daily cap, one connection, blacklist freeze."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from cnequity.adapters.baostock import access
from cnequity.adapters.baostock._session import fetch_per_symbol
from cnequity.adapters.baostock.access import (
    BLACKLIST_HOURS_PER_STRIKE,
    DAILY_REQUEST_LIMIT,
    EMPTY_RELEASE_REFRESH_SECONDS,
    admit_request,
    cooldown_seconds,
    hold_baostock_connection,
    record_blacklist,
    status,
)
from cnequity.config import Config, WaveConfig, validate_config
from cnequity.domain.http_policy import SourceCoolingDown, cooldown_status

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _cfg(tmp_path) -> Config:
    return Config(data_root=tmp_path / "lake", source_intervals={"baostock": 0})


def _at(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=_SHANGHAI).timestamp()


def test_unknown_release_waits_the_published_freeze_not_a_refresh():
    assert cooldown_seconds(1, release_at=None, now=0) == BLACKLIST_HOURS_PER_STRIKE * 3600
    assert cooldown_seconds(2, release_at=None, now=0) == 2 * BLACKLIST_HOURS_PER_STRIKE * 3600
    assert cooldown_seconds(1, release_at=None, now=0) >= EMPTY_RELEASE_REFRESH_SECONDS
    # A vendor time sooner than the formula is not a reason to reconnect.
    assert cooldown_seconds(1, release_at=60, now=0) == BLACKLIST_HOURS_PER_STRIKE * 3600
    assert cooldown_seconds(1, release_at=10 * 3600, now=0) == 10 * 3600


def test_daily_cap_stops_before_the_request_and_resets_on_the_shanghai_day(tmp_path):
    cfg = _cfg(tmp_path)
    now = time.time()
    today = datetime.fromtimestamp(now, _SHANGHAI).date()
    path = cfg.rate_limit_root / "baostock-access.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"day": today.isoformat(), "requests": DAILY_REQUEST_LIMIT}),
        encoding="utf-8",
    )
    entered = []
    with pytest.raises(SourceCoolingDown, match="50000"):
        admit_request(cfg, now=now)
    with pytest.raises(SourceCoolingDown, match="50000"):
        with cfg.source_request("baostock"):
            entered.append(1)
    assert entered == []
    assert json.loads(path.read_text(encoding="utf-8"))["requests"] == DAILY_REQUEST_LIMIT

    nxt = datetime(today.year, today.month, today.day, 0, 5, tzinfo=_SHANGHAI) + timedelta(days=1)
    admit_request(cfg, now=nxt.timestamp())
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["day"] == nxt.date().isoformat()
    assert saved["requests"] == 1
    report = status(cfg)
    assert report["daily_limit"] == DAILY_REQUEST_LIMIT
    assert report["calendar"] == "Asia/Shanghai"


def test_blacklist_strikes_grow_through_the_year_and_reset(tmp_path):
    cfg = _cfg(tmp_path)
    first_at = _at(2026, 6, 1)
    first = record_blacklist(cfg, "黑名单用户", now=first_at)
    assert first["strikes"] == 1
    assert first["seconds"] == 6 * 3600
    assert first["release_known"] is False
    again = record_blacklist(cfg, "黑名单用户", now=first_at + 30)
    assert again["strikes"] == 1
    assert again["until"] == first["until"]

    second = record_blacklist(cfg, "", now=first["until"] + 1)
    assert second["strikes"] == 2
    assert second["seconds"] == 12 * 3600

    now = second["until"] + 1
    release_at = _at(2026, 6, 3)
    released = record_blacklist(cfg, "待释放时间 2026-06-03 00:00:00", now=now)
    assert released["strikes"] == 3
    assert released["release_known"] is True
    assert released["seconds"] == pytest.approx(release_at - now)
    assert released["seconds"] > 3 * BLACKLIST_HOURS_PER_STRIKE * 3600

    nxt = record_blacklist(cfg, "", now=_at(2027, 1, 2))
    assert nxt["strikes"] == 1
    assert nxt["seconds"] == 6 * 3600
    assert cooldown_status(cfg.rate_limit_root, "baostock")["kind"] == "ip_blacklist"


def test_configured_concurrency_cannot_open_a_second_baostock_connection(tmp_path):
    from cnequity.adapters.throttle import SourceRateLimiters
    from cnequity.diagnostics.source_limits import effective_source_policy

    cfg = Config(
        data_root=tmp_path / "lake",
        workers=1,
        source_concurrency={"baostock": 4},
        daily_waves=[WaveConfig(name="reference", parallel=True, steps=["instruments"])],
    )
    assert SourceRateLimiters(cfg)._concurrency["baostock"].limit == 1
    assert effective_source_policy(cfg, "baostock")["configured_max_concurrency"] == 1
    assert any("baostock" in error and "one connection" in error for error in validate_config(cfg))


def test_a_second_connection_does_not_login(tmp_path, monkeypatch):
    monkeypatch.setattr(access, "CONNECTION_WAIT_SECONDS", 0.2)
    cfg = _cfg(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def hold():
        with hold_baostock_connection(cfg):
            started.set()
            release.wait(5)

    class Session:
        def __init__(self):
            self.logins = 0

        def login(self):
            self.logins += 1
            return type("R", (), {"error_code": "0", "error_msg": ""})()

        def logout(self):
            return None

    bs = Session()
    thread = threading.Thread(target=hold)
    thread.start()
    assert started.wait(2)
    try:
        with pytest.raises(SourceCoolingDown, match="并发"):
            fetch_per_symbol(
                ["600000.SH"],
                datetime(2020, 1, 1).date(),
                datetime(2020, 1, 2).date(),
                lambda *_: [{"symbol": "600000.SH"}],
                bs=bs,
                config=cfg,
                sleep=lambda _: None,
            )
        assert bs.logins == 0
    finally:
        release.set()
        thread.join(5)


def test_session_calls_keep_the_sdk_banner_off_stdout(capsys):
    from cnequity.adapters.baostock._session import _session_call

    def login():
        print("login success!")
        return "ok"

    assert _session_call(login, config=None, label="login", deadline=5.0) == "ok"
    assert "login success" not in capsys.readouterr().out
