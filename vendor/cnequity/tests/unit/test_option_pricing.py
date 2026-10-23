"""Black-76 and BAW against published values, parity and round trips."""

from __future__ import annotations

import numpy as np
import pytest

from cnequity.domain.option_pricing import (
    baw,
    black76,
    greeks,
    implied_vol,
    norm_cdf,
)


def test_normal_cdf_is_accurate():
    assert norm_cdf(np.array([0.0]))[0] == pytest.approx(0.5, abs=1e-7)
    assert norm_cdf(np.array([1.96]))[0] == pytest.approx(0.9750021, abs=1e-6)
    assert norm_cdf(np.array([-1.0]))[0] == pytest.approx(0.1586553, abs=1e-6)


def test_black76_matches_hull():
    # Hull, Options, Futures and Other Derivatives, Example 18.6 (futures
    # option): F=20, K=20, r=9%, sigma=25%, T=4/12 → European put 1.12.
    put = black76(20.0, 20.0, 4 / 12, 0.09, 0.25, False)
    assert float(put) == pytest.approx(1.12, abs=0.005)


def test_put_call_parity_holds_for_black76():
    f, k, t, r, s = 105.0, 100.0, 0.5, 0.03, 0.3
    call = float(black76(f, k, t, r, s, True))
    put = float(black76(f, k, t, r, s, False))
    assert call - put == pytest.approx(np.exp(-r * t) * (f - k), abs=1e-9)


def _american_tree(f, k, t, r, sigma, is_call, steps=2000):
    """Cox-Ross-Rubinstein lattice for an American option on a futures price."""
    dt = t / steps
    up = np.exp(sigma * np.sqrt(dt))
    p = (1 - 1 / up) / (up - 1 / up)
    disc = np.exp(-r * dt)
    prices = f * up ** np.arange(steps, -steps - 1, -2)
    values = np.maximum(prices - k, 0) if is_call else np.maximum(k - prices, 0)
    for _ in range(steps):
        prices = prices[:-1] / up
        cont = disc * (p * values[:-1] + (1 - p) * values[1:])
        exercise = np.maximum(prices - k, 0) if is_call else np.maximum(k - prices, 0)
        values = np.maximum(cont, exercise)
    return float(values[0])


@pytest.mark.parametrize(
    ("f", "k", "t", "r", "sigma", "is_call"),
    [
        (100.0, 100.0, 0.25, 0.08, 0.15, True),
        (90.0, 100.0, 0.5, 0.08, 0.25, False),
        (110.0, 100.0, 1.0, 0.05, 0.35, True),
        (100.0, 120.0, 0.75, 0.03, 0.30, False),
    ],
)
def test_baw_agrees_with_a_binomial_lattice(f, k, t, r, sigma, is_call):
    approx = float(baw(f, k, t, r, sigma, is_call))
    exact = _american_tree(f, k, t, r, sigma, is_call)
    assert approx == pytest.approx(exact, rel=0.01, abs=0.01)


def test_american_is_never_worth_less_than_european_or_exercise():
    f = np.array([60.0, 90.0, 100.0, 110.0, 160.0])
    for is_call in (True, False):
        american = baw(f, 100.0, 0.5, 0.05, 0.25, is_call)
        european = black76(f, 100.0, 0.5, 0.05, 0.25, is_call)
        intrinsic = np.maximum(f - 100.0, 0) if is_call else np.maximum(100.0 - f, 0)
        assert np.all(american >= european - 1e-9)
        assert np.all(american >= intrinsic - 1e-9)


@pytest.mark.parametrize("model", ["black76", "baw"])
def test_implied_vol_round_trips(model):
    from cnequity.domain.option_pricing import price

    f = np.array([95.0, 100.0, 105.0, 100.0])
    k = np.array([100.0, 100.0, 100.0, 120.0])
    is_call = np.array([True, False, True, False])
    sigma = np.array([0.18, 0.25, 0.4, 0.3])
    target = price(model, f, k, 0.3, 0.02, sigma, is_call)
    solved, status = implied_vol(model, target, f, k, 0.3, 0.02, is_call)
    assert list(status) == ["ok"] * 4
    np.testing.assert_allclose(solved, sigma, atol=1e-6)


def test_a_price_under_intrinsic_has_no_volatility():
    solved, status = implied_vol("baw", np.array([1.0]), 120.0, 100.0, 0.3, 0.02, True)
    assert status[0] == "below_intrinsic" and np.isnan(solved[0])


def test_bumped_greeks_agree_with_analytic_ones_where_both_apply():
    args = (100.0, 105.0, 0.4, 0.03, 0.22, True)
    analytic = greeks("black76", *args)
    # r = 0: BAW collapses to Black-76, so its bumped Greeks must match.
    bumped = greeks("baw", 100.0, 105.0, 0.4, 0.0, 0.22, True)
    zero_rate = greeks("black76", 100.0, 105.0, 0.4, 0.0, 0.22, True)
    for key in ("delta", "gamma", "vega"):
        assert float(bumped[key]) == pytest.approx(float(zero_rate[key]), rel=1e-3)
    assert 0 < float(analytic["delta"]) < 1
