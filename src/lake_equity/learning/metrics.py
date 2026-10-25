"""Risk-adjusted performance statistics shared by backtests and studies."""
from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np

_NORMAL = NormalDist()


def _moments(returns):
    values = np.asarray(returns, dtype=float)
    values = values[np.isfinite(values)]
    if len(values) < 3:
        return None
    mean = float(values.mean())
    std = float(values.std(ddof=1))
    if std <= 0:
        return None
    centered = values - mean
    m2 = float(np.mean(centered ** 2))
    m3 = float(np.mean(centered ** 3))
    m4 = float(np.mean(centered ** 4))
    return mean, std, m3 / m2 ** 1.5, m4 / m2 ** 2


def probabilistic_sharpe_ratio(returns, benchmark=0.0):
    """P(SR > benchmark) under the Bailey/López de Prado approximation.

    ``returns`` are per-period (daily) returns; the benchmark is also per-period.
    """
    moments = _moments(returns)
    if moments is None:
        return None
    mean, std, skew, kurtosis = moments
    sr = mean / std
    denominator = 1.0 - skew * sr + ((kurtosis - 1.0) / 4.0) * sr * sr
    if denominator <= 0:
        return None
    z = (sr - float(benchmark)) * math.sqrt(len(np.asarray(returns, dtype=float)) - 1) / math.sqrt(denominator)
    return _NORMAL.cdf(z)


def deflated_sharpe_ratio(returns, trials, sharpe_variance=None):
    """DSR: PSR with the expected maximum Sharpe of ``trials`` independent tests.

    ``sharpe_variance`` is the cross-trial variance of non-annualized Sharpe;
    when unknown, the null variance of the Sharpe estimator is used.
    """
    moments = _moments(returns)
    if moments is None or not isinstance(trials, int) or trials < 1:
        return None
    mean, std, skew, kurtosis = moments
    sr = mean / std
    if trials == 1:
        expected_max = 0.0
    else:
        variance = float(sharpe_variance) if sharpe_variance and sharpe_variance > 0 else (1.0 + 0.5 * sr * sr)
        gamma = 0.5772156649015329
        expected_max = math.sqrt(variance) * (
            (1.0 - gamma) * _NORMAL.inv_cdf(1.0 - 1.0 / trials)
            + gamma * _NORMAL.inv_cdf(1.0 - 1.0 / (trials * math.e))
        )
    denominator = 1.0 - skew * sr + ((kurtosis - 1.0) / 4.0) * sr * sr
    if denominator <= 0:
        return None
    z = (sr - expected_max) * math.sqrt(len(np.asarray(returns, dtype=float)) - 1) / math.sqrt(denominator)
    return _NORMAL.cdf(z)


def sharpe_standard_error(returns):
    """Lo (2002) large-sample standard error of the non-annualized Sharpe."""
    moments = _moments(returns)
    if moments is None:
        return None
    mean, std, skew, kurtosis = moments
    sr = mean / std
    n = len(np.asarray(returns, dtype=float))
    variance = (1.0 - skew * sr + ((kurtosis - 1.0) / 4.0) * sr * sr) / (n - 1)
    return math.sqrt(variance) if variance > 0 else None
