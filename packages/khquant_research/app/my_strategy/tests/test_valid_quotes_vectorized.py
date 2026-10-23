"""Vectorized valid_quotes must equal row-wise _valid_ohlc on every edge case."""
import numpy as np
import pandas as pd
import pytest

from my_strategy.services.reference_runtime import _valid_ohlc, finite


def _row_wise(frame):
    return np.array([_valid_ohlc(bar) for bar in frame.to_dict("records")], dtype=bool)


def _vectorized(frame):
    ohlc = {field: pd.to_numeric(frame[field], errors="coerce").to_numpy(dtype=np.float64)
            for field in ("open", "high", "low", "close")}
    valid = np.ones(len(frame), dtype=bool)
    for values in ohlc.values():
        valid &= np.isfinite(values) & (values > 0)
    with np.errstate(invalid="ignore"):
        valid &= (ohlc["high"] >= np.maximum(ohlc["open"], np.maximum(ohlc["close"], ohlc["low"]))) & \
                 (ohlc["low"] <= np.minimum(ohlc["open"], np.minimum(ohlc["close"], ohlc["high"])))
    return valid


@pytest.mark.parametrize("quotes", [
    (10.0, 10.5, 9.5, 10.2),      # normal
    (10.0, 10.0, 10.0, 10.0),     # flat
    (0.0, 10.5, 9.5, 10.2),       # zero open
    (-1.0, 10.5, 9.5, 10.2),      # negative open
    (10.0, np.nan, 9.5, 10.2),    # NaN high
    (10.0, np.inf, 9.5, 10.2),    # inf high
    (10.0, 10.2, 9.5, 10.5),      # close above high
    (10.0, 10.5, 10.2, 10.1),     # low above close... high<low region
    (10.2, 10.5, 9.8, 9.6),       # open above close
    (10.0, 10.5, -0.1, 10.2),     # negative low
    (np.nan, np.nan, np.nan, np.nan),
])
def test_valid_quotes_matches_row_wise(quotes):
    frame = pd.DataFrame([quotes], columns=["open", "high", "low", "close"])
    assert _vectorized(frame)[0] == _row_wise(frame)[0]


def test_valid_quotes_matches_row_wise_mixed_frame():
    rng = np.random.default_rng(11)
    base = rng.uniform(8, 12, size=(300, 4))
    base[::17, 1] = np.nan
    base[::29, 0] = np.inf
    base[::31, 2] = -1.0
    # Force range violations: high below close, low above open.
    base[::13, 1] = base[::13].min(axis=1) - 0.01
    base[::19, 2] = base[::19].max(axis=1) + 0.01
    frame = pd.DataFrame(base, columns=["open", "high", "low", "close"])
    assert (_vectorized(frame) == _row_wise(frame)).all()


def test_finite_semantics_reference():
    assert finite(np.nan) is None and finite(np.inf) is None
    assert finite(True) is None and finite(np.bool_(True)) is None
    assert finite("12.5") == 12.5 and finite("x") is None and finite(None) is None
