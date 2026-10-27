"""All training/study index rows reach filtering before the merged API page."""
from contextlib import closing
import json
import sqlite3

import pytest

from lake_equity.learning.service import LearningService
from lake_equity.learning.study import StudyService
from test_api_integration import integrated_app  # noqa: F401


@pytest.mark.parametrize("service_type,table,key,prefix,kind,read_path", [
    (LearningService, "runs", "run_id", "learn", "factor_training", "/api/v1/learning/runs"),
    (StudyService, "studies", "study_id", "study", "research_study", "/api/v1/learning/studies"),
])
def test_training_and_study_search_reaches_oldest_real_sqlite_index_entry(
        integrated_app, service_type, table, key, prefix, kind, read_path):
    client, app, settings = integrated_app
    service = service_type(settings.root)
    service.index.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(service.index)) as connection:
        connection.execute(f"CREATE TABLE {table}({key} TEXT PRIMARY KEY,summary TEXT)")
        for index in range(105):
            record = {key: f"{prefix}-{index:032x}", "created_at": index, "status": "completed",
                      "title": "oldest-index-needle" if index == 0 else "newer indexed report"}
            connection.execute(f"INSERT INTO {table} VALUES(?,?)", (record[key], json.dumps(record)))
        connection.commit()
    # Preserve the existing bounded direct list contract.
    assert len(next(iter(service.list_runs().values()))) == 100
    assert len(next(iter(service.list_runs(limit=None).values()))) == 105
    assert len(next(iter(client.get(read_path).json().values()))) == 100
    result = client.get(f"/api/v1/reports?kind={kind}&q={prefix}-{'0' * 32}&status=completed").json()
    assert result["total"] == 1
    assert result["reports"][0]["run_id"] == f"{prefix}-{'0' * 32}"
    final = client.get(f"/api/v1/reports?kind={kind}&limit=20&offset=100").json()
    assert final["total"] == 105 and len(final["reports"]) == 5
    assert final["has_more"] is False


def test_backtest_filter_preserves_saved_historical_simulation_reports(integrated_app):
    client, app, settings = integrated_app
    path = settings.metadata_root / "light_research/research.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    identity = "a" * 64
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE results(cache_key TEXT,created_at REAL,namespace TEXT)")
        connection.execute("INSERT INTO results VALUES(?,?,?)", (identity, 12345., "simulations"))
        connection.commit()
    assert app.state.history.reports(kind="backtest", limit=None)["reports"][0]["run_id"] == identity
    response = client.get("/api/v1/reports?kind=backtest&status=saved").json()
    assert response["total"] == 1
    assert response["reports"][0]["kind"] == "simulation"
    assert response["reports"][0]["historical_read_only"] is True
    assert response["reports"][0]["run_id"] == identity
