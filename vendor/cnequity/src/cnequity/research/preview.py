"""Symbol files for daily bars sealed during a sweep, before the lake publish.

A full compact rewrites every date partition. Doing that after each batch would
make initialization slower. These files let ``load(\"daily_bars\", symbols=...)``
read finished symbols while the rest of the universe is still downloading.
A query without ``symbols`` stays on published data, so a partial sweep cannot
look like a complete market. Adjusted prices still wait for ``adj_factors``.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from cnequity.storage.atomic import write_parquet_atomic


def preview_root(config) -> Path:
    return Path(config.data_root) / "preview" / "daily_bars"


def _safe_symbol(symbol: object) -> str | None:
    text = str(symbol)
    if not text or text in {".", ".."} or "/" in text or "\\" in text:
        return None
    return text


def publish_daily_bar_preview(config, run_id: str, batch_id: str) -> list[str]:
    """Copy one sealed batch into per-symbol files. Returns the symbols written."""
    from cnequity.storage.parquet import StagingWriter

    path = (
        Path(config.staging_root) / "daily_bars" / f"run_id={run_id}" / f"part-{batch_id}.parquet"
    )
    writer = StagingWriter(config.staging_root)
    if not path.is_file() or not writer.is_sealed(path, "daily_bars", run_id):
        return []
    frame = pl.read_parquet(path)
    if frame.is_empty() or "symbol" not in frame.columns:
        return []
    root = preview_root(config)
    root.mkdir(parents=True, exist_ok=True)
    published: list[str] = []
    for key, group in frame.partition_by("symbol", as_dict=True).items():
        symbol = _safe_symbol(key[0] if isinstance(key, tuple) else key)
        if symbol is None:
            continue
        dest = root / f"{symbol}.parquet"
        if dest.is_file():
            previous = pl.read_parquet(dest)
            group = pl.concat([previous, group], how="diagonal_relaxed")
            group = group.unique(subset=["symbol", "trade_date"], keep="last")
        write_parquet_atomic(dest, group, compression="zstd")
        published.append(symbol)
    return published


def preview_symbols(config) -> list[str]:
    root = preview_root(config)
    if not root.is_dir():
        return []
    return sorted(path.stem for path in root.glob("*.parquet") if path.is_file())


def read_daily_bar_preview(config, symbols: list[str], start, end) -> pl.DataFrame:
    frames: list[pl.DataFrame] = []
    root = preview_root(config)
    for symbol in symbols:
        safe = _safe_symbol(symbol)
        if safe is None:
            continue
        path = root / f"{safe}.parquet"
        if not path.is_file():
            continue
        frame = pl.read_parquet(path)
        if frame.is_empty() or "trade_date" not in frame.columns:
            continue
        if start is not None:
            frame = frame.filter(pl.col("trade_date") >= start)
        if end is not None:
            frame = frame.filter(pl.col("trade_date") <= end)
        if not frame.is_empty():
            frames.append(frame)
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="diagonal_relaxed")


def retire_preview_symbols(config, symbols: list[str]) -> None:
    """Drop preview files whose symbols are now in the published lake."""
    root = preview_root(config)
    if not root.is_dir():
        return
    for symbol in symbols:
        safe = _safe_symbol(symbol)
        if safe is None:
            continue
        (root / f"{safe}.parquet").unlink(missing_ok=True)
