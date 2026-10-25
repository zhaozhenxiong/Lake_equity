from __future__ import annotations

import numpy as np

from lake_equity.learning.metrics import (
    deflated_sharpe_ratio,
    probabilistic_sharpe_ratio,
    sharpe_standard_error,
)


def test_psr_is_defined_and_increases_with_sample_size():
    rng = np.random.default_rng(42)
    small = rng.normal(0.0006, 0.01, 120)
    large = rng.normal(0.0006, 0.01, 1200)
    psr_small = probabilistic_sharpe_ratio(small)
    psr_large = probabilistic_sharpe_ratio(large)
    assert 0 < psr_small < 1
    assert 0 < psr_large < 1
    assert psr_large > psr_small
    assert sharpe_standard_error(large) < sharpe_standard_error(small)


def test_deflated_sharpe_is_stricter_than_psr_with_many_trials():
    rng = np.random.default_rng(7)
    returns = rng.normal(0.0008, 0.01, 500)
    psr = probabilistic_sharpe_ratio(returns)
    dsr = deflated_sharpe_ratio(returns, trials=20)
    assert dsr is not None
    assert 0 <= dsr <= psr < 1
    assert dsr < 0.999


def test_short_or_constant_series_cannot_produce_sharpe_statistics():
    assert probabilistic_sharpe_ratio([0.01, -0.01]) is None
    assert probabilistic_sharpe_ratio([0.0] * 100) is None
    assert deflated_sharpe_ratio([0.01, -0.01], trials=3) is None
    assert sharpe_standard_error([0.01, -0.01]) is None
