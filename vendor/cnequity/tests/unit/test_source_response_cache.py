from datetime import date, datetime, timezone

import pytest

from cnequity.config import Config
from cnequity.steps.common import load_negative_evidence, record_negative_evidence
from cnequity.storage.state import StateStore


@pytest.mark.parametrize("legacy_version", [None, 2])
def test_legacy_daily_bar_empty_votes_are_invalidated(tmp_path, monkeypatch, legacy_version):
    cfg = Config(data_root=tmp_path / "lake", negative_evidence_ttl_days=7)
    clock = datetime(2026, 10, 11, tzinfo=timezone.utc)
    monkeypatch.setattr("cnequity.steps.common._instrument_identity", lambda *args: {"catalog": 1})
    entry = {
        "symbol": "600519.SH",
        "window_start": "2026-10-09",
        "window_end": "2026-10-09",
        "source": "eastmoney+sina",
        "reason": "verified_no_data",
    }
    legacy_identity = {"catalog": 1}
    if legacy_version is not None:
        legacy_identity["source_response_contract"] = legacy_version
    StateStore(cfg.meta_root).record_negative_evidence(
        "daily_bars", [entry], ttl_days=7, identity=legacy_identity, now=clock
    )
    assert load_negative_evidence(cfg, "daily_bars", now=clock) == []
    record_negative_evidence(
        cfg,
        "daily_bars",
        ["600519.SH"],
        date(2026, 10, 9),
        date(2026, 10, 9),
        reason="verified_no_data",
        source="eastmoney+sina",
        now=clock,
    )
    evidence = load_negative_evidence(cfg, "daily_bars", now=clock)
    assert len(evidence) == 1
    assert evidence[0]["identity"]["source_response_contract"] == 3
