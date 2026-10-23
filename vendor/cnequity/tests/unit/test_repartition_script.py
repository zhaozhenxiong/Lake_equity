"""`scripts/migrations/repartition.py` — the former `cne repartition`.

It moved because what triggers it is a registry granularity change landing on an
existing lake: a migration, alongside `migrate_daily_bars_volume_v2.py`, not a
daily operation. Reads already self-describe from the directory shape, so this
only reclaims space and file handles — it never fixes a correctness problem.

`cnequity.quality.dataset_checks` names the script in the findings it emits, so
`test_curated_hygiene` covers that the advice still points somewhere real.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from cnequity.config.bootstrap import path_for_toml
from cnequity.storage.repairs.repartition import RepartitionResult

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "migrations" / "repartition.py"


@pytest.fixture
def repartition():
    spec = importlib.util.spec_from_file_location("repartition_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cfg_path(tmp_path):
    path = tmp_path / "cnequity.toml"
    path.write_text(f'[data]\nroot = "{path_for_toml(tmp_path / "data")}"\n', encoding="utf-8")
    return str(path)


def _result(name: str) -> RepartitionResult:
    return RepartitionResult(
        dataset=name,
        changed=True,
        rows=10,
        files_before=5,
        files_after=1,
        partitions_before=5,
        partitions_after=1,
        bytes_before=1_000_000,
        bytes_after=200_000,
    )


def test_bare_invocation_only_lists_and_never_writes(repartition, cfg_path, monkeypatch, capsys):
    """The listing form has to stay safe to run: it is what the hygiene findings
    tell an operator to try first."""
    monkeypatch.setattr(repartition, "repartition_candidates", lambda cfg: ["index_bars"])
    monkeypatch.setattr(
        repartition,
        "repartition_dataset",
        lambda *a, **k: pytest.fail("listing must not rewrite anything"),
    )

    assert repartition.main(["--config", cfg_path]) == 0
    assert json.loads(capsys.readouterr().out)["needs_repartition"] == ["index_bars"]


def test_dry_run_reports_the_effect(repartition, cfg_path, monkeypatch, capsys):
    monkeypatch.setattr(repartition, "repartition_candidates", lambda cfg: ["index_bars"])
    monkeypatch.setattr(
        repartition,
        "repartition_dataset",
        lambda cfg, name, dry_run=False: _result(name),
    )

    assert repartition.main(["index_bars", "--dry-run", "--config", cfg_path]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["results"][0]["dataset"] == "index_bars"
    assert payload["results"][0]["mb_saved"] == 0.8


def test_all_walks_every_candidate(repartition, cfg_path, monkeypatch, capsys):
    seen: list[str] = []
    monkeypatch.setattr(
        repartition, "repartition_candidates", lambda cfg: ["index_bars", "trading_calendar"]
    )
    monkeypatch.setattr(
        repartition,
        "repartition_dataset",
        lambda cfg, name, dry_run=False: (seen.append(name), _result(name))[1],
    )

    assert repartition.main(["--all", "--config", cfg_path]) == 0
    assert seen == ["index_bars", "trading_calendar"]


def test_a_dataset_and_all_together_is_refused(repartition, cfg_path, capsys):
    """They mean different target sets; picking one silently would rewrite either
    more or less of the lake than asked."""
    assert repartition.main(["index_bars", "--all", "--config", cfg_path]) == 1
    assert "not both" in capsys.readouterr().err


def test_a_repartition_error_is_reported_not_raised(repartition, cfg_path, monkeypatch, capsys):
    from cnequity.storage.repairs.repartition import RepartitionError

    monkeypatch.setattr(repartition, "repartition_candidates", lambda cfg: ["index_bars"])

    def _boom(cfg, name, dry_run=False):
        raise RepartitionError("instruments is not partitioned by date")

    monkeypatch.setattr(repartition, "repartition_dataset", _boom)

    assert repartition.main(["instruments", "--config", cfg_path]) == 1
    assert "not partitioned by date" in capsys.readouterr().err
