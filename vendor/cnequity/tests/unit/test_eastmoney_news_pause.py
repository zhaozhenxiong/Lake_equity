"""Fast-news lane: repeated refusals close it for the local day."""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import polars as pl
import pytest

from cnequity.adapters.eastmoney import news_feed as adapter
from cnequity.adapters.eastmoney import news_pause
from cnequity.config import Config, load_config
from cnequity.config.bootstrap import path_for_toml
from cnequity.domain.http_policy import SourceCoolingDown
from cnequity.orchestrator.outcomes import SourceUnavailableError
from cnequity.steps import news_feed as step

_COOLING = SourceCoolingDown("eastmoney: HTTP 403; shared cooldown")


@pytest.fixture(autouse=True)
def _fresh_fetch_cache():
    """`shared_fetch` caches by run id for the process; tests reuse ids like `run-2`."""
    step._CACHE.clear()
    step._STAGED.clear()
    yield
    step._CACHE.clear()
    step._STAGED.clear()


def _cfg(tmp_path, **kwargs) -> Config:
    return Config(data_root=tmp_path / "data", **kwargs)


def _response(status: int) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("GET", "https://np-listapi.eastmoney.com/"))


def _refused_403() -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError("403", request=_response(403).request, response=_response(403))


def test_third_refused_sweep_in_a_row_closes_the_lane_until_tomorrow(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    news_pause.ensure_open(cfg)
    news_pause.note_outcome(cfg, _COOLING)
    news_pause.note_outcome(cfg, _COOLING)
    news_pause.ensure_open(cfg)  # two strikes: still open
    news_pause.note_outcome(cfg, _COOLING)

    with pytest.raises(news_pause.NewsPausedError, match="until tomorrow"):
        news_pause.ensure_open(cfg)
    assert isinstance(news_pause.NewsPausedError("x"), SourceUnavailableError)
    state = news_pause.status(cfg)
    assert state["paused"]["strikes"] == 3 and state["paused"]["reason"] == "SourceCoolingDown"

    tomorrow = date.today() + timedelta(days=1)
    monkeypatch.setattr(news_pause, "_today", lambda: tomorrow)
    news_pause.ensure_open(cfg)  # a new local day probes again
    assert news_pause.status(cfg) == {
        "breaker_policy": True,
        "strikes": 0,
        "strikes_needed": 3,
        "paused": None,
    }


def test_a_success_clears_the_count_so_only_consecutive_refusals_trip(tmp_path):
    cfg = _cfg(tmp_path)
    for _ in range(5):
        news_pause.note_outcome(cfg, _COOLING)
        news_pause.note_outcome(cfg, _refused_403())
        news_pause.note_outcome(cfg, None)
    news_pause.ensure_open(cfg)
    assert news_pause.status(cfg)["strikes"] == 0


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("proxy port closed"),
        httpx.ProxyError("clash down"),
        httpx.ReadTimeout("slow"),
        httpx.HTTPStatusError("502", request=_response(502).request, response=_response(502)),
        RuntimeError("payload shape"),
    ],
)
def test_only_refusals_count(tmp_path, exc):
    cfg = _cfg(tmp_path)
    for _ in range(10):
        news_pause.note_outcome(cfg, exc)
    news_pause.ensure_open(cfg)
    assert not (cfg.rate_limit_root / "eastmoney_news_pause.json").exists()


def test_an_http_403_or_429_counts_like_a_cooldown(tmp_path):
    cfg = _cfg(tmp_path, eastmoney_news_breaker_strikes=2)
    news_pause.note_outcome(cfg, _refused_403())
    news_pause.ensure_open(cfg)
    news_pause.note_outcome(cfg, _refused_403())
    with pytest.raises(news_pause.NewsPausedError):
        news_pause.ensure_open(cfg)


def test_the_breaker_can_be_switched_off(tmp_path):
    cfg = _cfg(tmp_path, eastmoney_news_breaker=False)
    for _ in range(10):
        news_pause.note_outcome(cfg, _COOLING)
    news_pause.ensure_open(cfg)
    assert not (cfg.rate_limit_root / "eastmoney_news_pause.json").exists()


def test_config_defaults_and_toml_override(tmp_path):
    path = tmp_path / "cnequity.toml"
    path.write_text(
        f'[data]\nroot = "{path_for_toml(tmp_path / "data")}"\n[sources.eastmoney]\nenabled = true\n',
        "utf-8",
    )
    cfg = load_config(path)
    assert (cfg.eastmoney_news_breaker, cfg.eastmoney_news_breaker_strikes) == (True, 3)

    path.write_text(
        f'[data]\nroot = "{path_for_toml(tmp_path / "data")}"\n[sources.eastmoney]\nenabled = true\n'
        "news_breaker = false\nnews_breaker_strikes = 5\n",
        "utf-8",
    )
    cfg = load_config(path)
    assert (cfg.eastmoney_news_breaker, cfg.eastmoney_news_breaker_strikes) == (False, 5)


def test_shared_fetch_stops_sending_once_the_lane_is_closed(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    sent = []

    def refused(since, *, config=None, recorder=None, **_):
        sent.append(since)
        raise _COOLING

    monkeypatch.setattr(adapter, "fetch_news_since", refused)
    for n in range(3):
        with pytest.raises(SourceCoolingDown):
            step.shared_fetch(cfg, date(2026, 10, 18), f"run-{n}")
    assert len(sent) == 3

    for n in range(3, 6):  # the following ticks never reach the network
        with pytest.raises(news_pause.NewsPausedError):
            step.shared_fetch(cfg, date(2026, 10, 18), f"run-{n}")
    assert len(sent) == 3


def test_shared_fetch_success_resets_the_count(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    outcomes = [_COOLING, _COOLING, None, _COOLING, _COOLING]

    def fetch(since, *, config=None, recorder=None, **_):
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome
        return pl.DataFrame(schema=adapter.NEWS_COLUMNS), True

    monkeypatch.setattr(adapter, "fetch_news_since", fetch)
    for n in range(5):
        try:
            step.shared_fetch(cfg, date(2026, 10, 18), f"run-{n}")
        except SourceCoolingDown:
            pass
    assert not outcomes
    news_pause.ensure_open(cfg)  # 2 + success + 2 refusals: never 3 in a row
    assert news_pause.status(cfg)["strikes"] == 2
