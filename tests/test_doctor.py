import sqlite3

import pytest

from lake_equity.cli import doctor
from lake_equity.settings import Settings


@pytest.fixture
def healthy_runtime(tmp_path, monkeypatch):
    import lake_equity.cli
    from my_strategy.adapters import czsc_adapter
    from my_strategy.services import czsc_trend_support, kronos_assets

    monkeypatch.delenv("LAKE_EQUITY_RAW_DB", raising=False)
    settings = Settings(tmp_path)
    settings.raw_db.parent.mkdir(parents=True)
    with sqlite3.connect(settings.raw_db) as connection:
        connection.execute("CREATE TABLE stock_daily_normalized(stock TEXT,date TEXT)")
        connection.execute("INSERT INTO stock_daily_normalized VALUES('600000.SH','2026-10-22')")
    monkeypatch.setattr(lake_equity.cli.importlib.metadata, "version", lambda package: "synthetic-runtime")
    monkeypatch.setattr(czsc_adapter, "native_runtime", lambda: object())
    monkeypatch.setattr(czsc_adapter, "engine_contract", lambda: {"schema": "synthetic-frozen-engine"})
    monkeypatch.setattr(kronos_assets, "asset_status", lambda: {"ready": True, "missing": [], "errors": []})
    monkeypatch.setattr(czsc_trend_support, "_calendar", lambda: (["2026-10-22", "2026-10-23"], "synthetic-calendar"))
    return settings


def test_doctor_is_ok_only_when_runtime_assets_and_database_check_pass(healthy_runtime):
    result = doctor(healthy_runtime)
    assert result["status"] == "ok"
    assert result["checks"]["kronos_assets"]["available"] is True
    assert result["checks"]["raw_database"]["available"] is True
    assert result["checks"]["raw_database"]["quick_check"] == "ok"


@pytest.mark.parametrize("assets", [
    {"ready": False, "missing": ["model.safetensors"], "errors": []},
    {"missing": ["model.safetensors"], "errors": []},
])
def test_missing_kronos_assets_cannot_report_a_healthy_runtime(healthy_runtime, monkeypatch, assets):
    from my_strategy.services import kronos_assets

    monkeypatch.setattr(kronos_assets, "asset_status", lambda: assets)
    result = doctor(healthy_runtime)
    assert result["status"] == "partial"
    assert result["checks"]["kronos_assets"]["available"] is False
    assert result["checks"]["kronos_assets"]["missing"] == ["model.safetensors"]
    assert result["checks"]["raw_database"]["available"] is True


def test_non_ok_sqlite_quick_check_cannot_report_a_healthy_runtime(healthy_runtime, monkeypatch):
    import lake_equity.cli

    class SyntheticCorruptionConnection:
        def execute(self, sql):
            self.sql = sql
            return self

        def fetchone(self):
            if self.sql == "PRAGMA quick_check":
                return ("synthetic database corruption",)
            return (1, 1, "2026-10-22", "2026-10-22")

        def close(self):
            pass

    monkeypatch.setattr(lake_equity.cli.sqlite3, "connect", lambda *args, **kwargs: SyntheticCorruptionConnection())
    result = doctor(healthy_runtime)
    assert result["status"] == "partial"
    assert result["checks"]["raw_database"]["available"] is False
    assert result["checks"]["raw_database"]["quick_check"] == "synthetic database corruption"
    assert result["checks"]["kronos_assets"]["available"] is True
