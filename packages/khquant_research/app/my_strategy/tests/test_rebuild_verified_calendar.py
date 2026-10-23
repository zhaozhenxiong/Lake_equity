"""Verified-calendar rebuild script and infrastructure protection contracts."""
import json

import pandas as pd
import pytest

from my_strategy.scripts.rebuild_verified_calendar import rebuild_calendar_run
from my_strategy.services.czsc_research_calendar import (
    is_infrastructure_run, load_verified_calendar)


def _sessions():
    return pd.bdate_range("2026-01-01", "2026-03-31").strftime("%Y-%m-%d").tolist()


def test_rebuild_validates_against_loader_and_marks_infrastructure(tmp_path):
    dates = _sessions()
    meta = rebuild_calendar_run(
        tmp_path, "calendar_bootstrap_test", start="2026-01-01", end="2026-03-31",
        akshare_sessions=dates, sina_sessions=list(dates), observed_end="2026-03-31")
    assert meta["session_count"] == len(dates)
    assert meta["first_session"] == dates[0] and meta["last_session"] == dates[-1]
    assert meta["independent_from_market_bars"] is True

    loaded_dates, loaded_meta = load_verified_calendar(
        "calendar_bootstrap_test", runs_root=tmp_path, require_sources=True)
    assert loaded_dates == dates
    assert loaded_meta["hash"] == meta["hash"]

    run_dir = tmp_path / "calendar_bootstrap_test"
    assert is_infrastructure_run(run_dir) is True
    marker = json.loads((run_dir / "infrastructure.json").read_text(encoding="utf-8"))
    assert marker["kind"] == "verified_calendar"
    assert (run_dir / "sources" / "installed_akshare_calendar.json").is_file()
    assert (run_dir / "sources" / "sina_klc_td_sh.txt").is_file()


def test_rebuild_refuses_mismatched_independent_sources(tmp_path):
    dates = _sessions()
    with pytest.raises(ValueError, match="独立日历来源不一致"):
        rebuild_calendar_run(
            tmp_path, "calendar_bootstrap_bad", start="2026-01-01", end="2026-03-31",
            akshare_sessions=dates, sina_sessions=dates[1:], observed_end="2026-03-31")
    assert not (tmp_path / "calendar_bootstrap_bad" / "reports" / "calendar.json").exists()


def test_missing_calendar_error_points_at_rebuild_script(tmp_path):
    with pytest.raises(FileNotFoundError, match="rebuild_verified_calendar.py"):
        load_verified_calendar("calendar_bootstrap_gone", runs_root=tmp_path)


def test_plain_run_dir_is_not_infrastructure(tmp_path):
    plain = tmp_path / "20261020-153941-456778-scan"
    plain.mkdir()
    assert is_infrastructure_run(plain) is False
    (plain / "infrastructure.json").write_text('{"infrastructure": false}', encoding="utf-8")
    assert is_infrastructure_run(plain) is False
    (plain / "infrastructure.json").write_text("not json", encoding="utf-8")
    assert is_infrastructure_run(plain) is False
