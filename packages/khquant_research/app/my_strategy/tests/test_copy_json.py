"""copy_json semantics and performance-contract tests."""
import copy

import numpy as np
import pandas as pd
import pytest

from my_strategy.services.czsc_trend_support import copy_json


def test_copy_json_matches_deepcopy_on_payloads():
    payload = {"a": [1, 2.5, "x", None, True, {"b": [3, {"c": ()}]}], "d": {"e": []}}
    copied = copy_json(payload)
    assert copied == payload and copied is not payload
    assert copied["a"] is not payload["a"] and copied["a"][5] is not payload["a"][5]
    # Immutable scalars are shared, exactly like deepcopy.
    assert copied["a"][2] is payload["a"][2]


def test_copy_json_falls_back_for_non_json_objects():
    stamp = pd.Timestamp("2026-10-20")
    array = np.array([1.0, 2.0])
    copied = copy_json({"t": stamp, "v": array})
    assert isinstance(copied["t"], pd.Timestamp) and copied["t"] == stamp
    assert isinstance(copied["v"], np.ndarray) and copied["v"] is not array
    assert np.array_equal(copied["v"], array)


def test_copy_json_output_equality_after_mutation():
    original = {"events": [{"id": 1, "tags": ["a"]}]}
    for clone in (copy_json(original), copy.deepcopy(original)):
        clone["events"][0]["tags"].append("b")
    assert original == {"events": [{"id": 1, "tags": ["a"]}]}
