"""Trailing daily factors on a calendar-aligned, single-security frame."""
from __future__ import annotations

import numpy as np
import pandas as pd


LEGACY_MA = [
    "ma5_bias", "ma10_bias", "ma20_bias", "ma60_bias",
    "ma5_slope5", "ma10_slope5", "ma20_slope5", "ma60_slope5",
    "ma_alignment", "close_above_ma20", "ret_1", "ret_5", "ret_20",
    "atr14_ratio", "volatility20", "drawdown20", "drawdown60",
    "ma5_ma10_gap", "ma10_ma20_gap", "ma20_ma60_gap",
]
PRICE_VOLUME = LEGACY_MA + [
    "ret_10", "ret_60", "volatility5", "volatility60", "range_ratio",
    "body_ratio", "upper_shadow", "lower_shadow", "overnight_gap",
    "vol_ratio5", "vol_ratio20", "vol_ma5_to20", "amount_ratio20",
    "amount_ma5_to20", "close_position20", "close_position60", "amihud20",
    "volume_change5",
]


def trailing_features(bars: pd.DataFrame) -> pd.DataFrame:
    """SMA baseline matches ma_trend_v1 formulas; no fitting or future reads.

    Missing sessions must already be present as NaN rows. An invalid bar is
    masked by the caller, so rolling windows cannot squeeze across that gap.
    """
    close, volume, amount = (bars[key] for key in ("close", "volume", "amount"))
    out = pd.DataFrame(index=bars.index)
    for period in (1, 5, 10, 20, 60):
        out[f"ret_{period}"] = close.pct_change(period, fill_method=None)
    for period in (5, 10, 20, 60):
        ma = close.rolling(period, min_periods=period).mean()
        out[f"ma{period}"] = ma
        out[f"ma{period}_bias"] = close / ma - 1
        out[f"ma{period}_slope5"] = ma / ma.shift(5) - 1
    out["ma_alignment"] = sum(np.sign(out[f"ma{a}"] - out[f"ma{b}"])
                              for a, b in ((5, 10), (10, 20), (20, 60))) / 3
    out["close_above_ma20"] = np.sign(close - out["ma20"])
    for short, long in ((5, 10), (10, 20), (20, 60)):
        out[f"ma{short}_ma{long}_gap"] = out[f"ma{short}"] / out[f"ma{long}"] - 1
    previous = close.shift(1)
    tr = pd.concat([bars.high - bars.low, (bars.high - previous).abs(),
                    (bars.low - previous).abs()], axis=1).max(axis=1)
    out["atr14_ratio"] = tr.rolling(14, min_periods=14).mean() / close
    for period in (5, 20, 60):
        out[f"volatility{period}"] = out.ret_1.rolling(period, min_periods=period).std(ddof=0)
    for period in (20, 60):
        out[f"drawdown{period}"] = close / close.rolling(period, min_periods=period).max() - 1
        low = bars.low.rolling(period, min_periods=period).min()
        high = bars.high.rolling(period, min_periods=period).max()
        out[f"close_position{period}"] = (close - low) / (high - low).replace(0, np.nan)
    for period in (5, 20):
        baseline = volume.shift(1).rolling(period, min_periods=period).mean().replace(0, np.nan)
        out[f"vol_ratio{period}"] = volume / baseline
    out["vol_ma5_to20"] = volume.rolling(5).mean() / volume.rolling(20).mean().replace(0, np.nan)
    out["amount_ratio20"] = amount / amount.shift(1).rolling(20).mean().replace(0, np.nan)
    out["amount_ma5_to20"] = amount.rolling(5).mean() / amount.rolling(20).mean().replace(0, np.nan)
    out["range_ratio"] = (bars.high - bars.low) / close
    out["body_ratio"] = (close - bars.open) / bars.open
    out["upper_shadow"] = (bars.high - pd.concat([bars.open, close], axis=1).max(axis=1)) / close
    out["lower_shadow"] = (pd.concat([bars.open, close], axis=1).min(axis=1) - bars.low) / close
    out["overnight_gap"] = bars.open / previous - 1
    out["amihud20"] = (out.ret_1.abs() / amount.replace(0, np.nan)).rolling(20).mean() * 1e8
    out["volume_change5"] = volume.pct_change(5, fill_method=None)
    return out[PRICE_VOLUME].replace([np.inf, -np.inf], np.nan)
