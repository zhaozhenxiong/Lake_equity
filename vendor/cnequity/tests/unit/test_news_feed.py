"""Fast news: one incremental fetch per run, no midnight gap, both datasets fed."""

from __future__ import annotations

from datetime import date, datetime, timezone

import httpx
import polars as pl
import pytest

from cnequity.config import Config
from cnequity.steps import news_feed

CST = news_feed.CST


@pytest.fixture(autouse=True)
def _fresh_fetch_cache():
    """`shared_fetch` caches by run id for the process; tests reuse ids like `run-2`."""
    news_feed._CACHE.clear()
    news_feed._STAGED.clear()
    yield
    news_feed._CACHE.clear()
    news_feed._STAGED.clear()


def _item(code: str, when: str, title: str = "消息") -> dict:
    return {
        "code": code,
        "showTime": when,
        "title": f"{title}{code}",
        "summary": "",
        "stockList": [],
    }


def _client(pages: list[list[dict]], requests: list[str]):
    class Client:
        def __init__(self, **kwargs):
            self.page = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, **kwargs):
            requests.append(url)
            items = pages[min(self.page, len(pages) - 1)]
            self.page += 1
            cursor = f"c{self.page}" if self.page < len(pages) else ""
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json={"data": {"fastNewsList": items, "sortEnd": cursor}},
            )

    return Client


def test_items_across_midnight_keep_their_own_dates(monkeypatch):
    """The old per-day fetch dropped what was published after the day's last run."""
    from cnequity.adapters.eastmoney import news_feed as adapter

    requests: list[str] = []
    pages = [
        [_item("3", "2026-09-27 00:05:00"), _item("2", "2026-09-26 23:58:00")],
        [_item("1", "2026-09-26 23:40:00")],
        [_item("0", "2026-09-26 20:00:00")],
    ]
    monkeypatch.setattr(adapter, "EastMoneyClient", _client(pages, requests))
    rows, reached = adapter.fetch_news_since(datetime(2026, 9, 26, 23, 45, tzinfo=CST))
    assert reached
    assert len(requests) == 2  # stops on the page that passed `since`
    assert rows.select("news_id", "publish_date").rows() == [
        ("3", date(2026, 9, 27)),
        ("2", date(2026, 9, 26)),
    ]


def test_a_walk_that_never_reaches_since_says_so(monkeypatch):
    from cnequity.adapters.eastmoney import news_feed as adapter

    requests: list[str] = []
    monkeypatch.setattr(
        adapter, "EastMoneyClient", _client([[_item("9", "2026-09-27 10:00:00")]], requests)
    )
    _rows, reached = adapter.fetch_news_since(datetime(2026, 9, 20, tzinfo=CST))
    assert not reached


def _seed(cfg: Config, dataset: str, when: str) -> None:
    day, clock = when.split("T")
    part = cfg.curated_root / dataset / f"publish_date={day}"
    part.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {"publish_date": [date.fromisoformat(day)], "publish_time": [clock]}
    ).write_parquet(part / "part-0.parquet")


def test_the_fetch_follows_the_canonical_news_watermark(tmp_path):
    cfg = Config(data_root=tmp_path / "data")
    assert news_feed.since_for(cfg, date(2026, 9, 27)) == datetime(2026, 9, 27, tzinfo=CST)
    _seed(cfg, "news_headlines", "2026-09-27T03:10:55")
    _seed(cfg, "flash_news_wire", "2026-09-26T23:00:00")
    # Legacy flash revisions do not force a duplicate wire fetch.
    assert news_feed.since_for(cfg, date(2026, 9, 27)) == datetime(
        2026, 9, 27, 2, 40, 55, tzinfo=CST
    )


def test_both_datasets_share_one_fetch_per_run(tmp_path, monkeypatch):
    from cnequity.adapters.eastmoney import news_feed as adapter
    from cnequity.steps import newsboard, rotation

    cfg = Config(data_root=tmp_path / "data", raw_archive_enabled=False)
    requests: list[str] = []
    monkeypatch.setattr(
        adapter,
        "EastMoneyClient",
        _client([[_item("1", "2026-09-27 09:00:00"), _item("0", "2026-09-26 08:00:00")]], requests),
    )
    written: list[tuple[str, int]] = []
    monkeypatch.setattr(
        "cnequity.steps.http_common.write_fetched",
        lambda config, run_id, dataset, df, **k: (
            written.append((dataset, df.height)) or {"rows_written": df.height}
        ),
    )
    news_feed._CACHE.clear()
    rotation.step_news_headlines(cfg, date(2026, 9, 27), "run-1", {})
    newsboard.step_flash_news_wire(cfg, date(2026, 9, 27), "run-1", {})
    assert len(requests) == 1
    assert written == [("news_headlines", 1)]


def test_an_empty_window_is_fine_once_the_lake_holds_news(tmp_path, monkeypatch):
    from cnequity.adapters.eastmoney import news_feed as adapter
    from cnequity.steps import newsboard

    cfg = Config(data_root=tmp_path / "data", raw_archive_enabled=False)
    monkeypatch.setattr(adapter, "EastMoneyClient", _client([[]], []))
    news_feed._CACHE.clear()
    with pytest.raises(RuntimeError, match="lake holds none"):
        newsboard.step_flash_news_wire(cfg, date(2026, 9, 27), "run-a", {})

    _seed(cfg, "flash_news_wire", "2026-09-27T03:00:00")
    _seed(cfg, "news_headlines", "2026-09-27T03:00:00")
    news_feed._CACHE.clear()
    assert newsboard.step_flash_news_wire(cfg, date(2026, 9, 27), "run-b", {})["rows_written"] == 0


def test_corrupt_canonical_staging_is_not_treated_as_completed_news(tmp_path, monkeypatch):
    cfg = Config(data_root=tmp_path / "data", raw_archive_enabled=False)
    path = cfg.staging_root / "news_headlines" / "run_id=run-bad" / "part-batch-0.parquet"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not parquet")
    monkeypatch.setattr(news_feed, "shared_fetch", lambda *_a: pytest.fail("must inspect staging"))
    with pytest.raises(RuntimeError, match="unreadable staging"):
        news_feed.stage_news(cfg, date(2026, 9, 27), "run-bad", "flash_news_wire")


def test_flash_wire_reads_canonical_news_and_legacy_rows(tmp_path):
    import duckdb

    from cnequity.adapters.eastmoney.news_wire import flash_rows_from_news
    from cnequity.query.reader import load
    from cnequity.query.views import ensure_duckdb_views

    cfg = Config(data_root=tmp_path / "data")
    now = datetime(2026, 9, 27, tzinfo=timezone.utc)

    def news_row(news_id: str) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "news_id": [news_id],
                "publish_date": [date(2026, 9, 27)],
                "publish_time": ["09:00:00"],
                "title": [f"headline {news_id}"],
                "summary": ["summary"],
                "related_symbols": [None],
                "channel": ["fast_news"],
                "source": ["eastmoney"],
                "data_version": ["v1"],
                "fetched_at": [now],
            }
        )

    canonical = cfg.curated_root / "news_headlines" / "publish_date=2026-09"
    canonical.mkdir(parents=True)
    news_row("new").write_parquet(canonical / "part-0.parquet")
    legacy = cfg.curated_root / "flash_news_wire" / "publish_date=2026-09"
    legacy.mkdir(parents=True)
    flash_rows_from_news(news_row("old")).write_parquet(legacy / "part-0.parquet")

    frame = load("flash_news_wire", config=cfg)
    assert set(frame["wire_id"]) == {"eastmoney:new", "eastmoney:old"}
    path = ensure_duckdb_views(cfg)
    with duckdb.connect(str(path), read_only=True) as conn:
        rows = conn.execute("SELECT wire_id FROM flash_news_wire ORDER BY wire_id").fetchall()
    assert rows == [("eastmoney:new",), ("eastmoney:old",)]
