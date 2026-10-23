"""_json exact-type fast path: output identical to the legacy branch chain."""
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from my_strategy.core.run_context import stable_hash
from my_strategy.services.czsc_trend_support import _json


def _json_legacy(value):
    """Pre-fast-path implementation, kept here as the semantic reference."""
    if isinstance(value, dict):
        return {str(k): _json_legacy(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_legacy(v) for v in value]
    if isinstance(value, np.ndarray):
        return [_json_legacy(v) for v in value.tolist()]
    if isinstance(value, (np.integer, np.bool_)):
        return value.item()
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, (pd.Timestamp, Path)):
        return str(value)
    return value


CASES = [
    None, True, False, 0, 1, -5, 2**63, "text", "", "中文", 3.14, -0.0, 1e300,
    float("nan"), float("inf"), -float("inf"),
    np.int64(7), np.int32(-3), np.uint8(2), np.float64(1.5), np.float32(2.5),
    np.float64("nan"), np.float32("inf"), np.bool_(True), np.bool_(False), np.str_("x"),
    np.array([1, 2, 3]), np.array([[1.0, np.nan], [np.inf, 0.0]]), np.array(["a", "b"]),
    np.array([True, False]), np.array([np.float32(1.25)], dtype=np.float32),
    pd.Timestamp("2026-10-19"), pd.Timestamp("2026-10-19 15:00:00+08:00"), Path("a/b"),
    [1, "a", None, [2.5, np.int64(2)]],
    (1, 2, (3, np.float32(1.25))),
    {}, [],
    {1: "int-key", "nested": {"list": [pd.Timestamp("2026-01-01"), Path("x")]}},
    {"mixed": [{"a": np.array([True, False])}, (np.bool_(False),)]},
    {"agents": {"ma": {"p_up": np.float64(0.42), "in_scope": np.bool_(True),
                       "reason_codes": ["a", "b"]}}},
]


@pytest.mark.parametrize("case", CASES)
def test_json_fast_path_matches_legacy(case):
    assert _json(case) == _json_legacy(case)
    assert stable_hash(_json(case)) == stable_hash(_json_legacy(case))


def test_json_fast_path_preserves_exact_float_type():
    # np.float64 is a float subclass; the legacy chain returned an exact float.
    converted = _json(np.float64(1.5))
    assert type(converted) is float and converted == 1.5


def test_json_fast_path_preserves_tuple_to_list_and_key_stringifying():
    assert _json({2: (1, 2)}) == {"2": [1, 2]}
