import time

import pytest
from click.testing import CliRunner

from cnequity.cli.main import cli
from cnequity.config import Config
from cnequity.config.bootstrap import path_for_toml
from cnequity.diagnostics import preflight
from cnequity.diagnostics.source_health import ProbeResult, ProbeStatus
from cnequity.orchestrator.engine import JobEngine


def _result(probe, status: ProbeStatus, detail: str = "") -> ProbeResult:
    return ProbeResult(
        key=probe.key,
        label=probe.label,
        host=probe.host,
        powers=list(probe.powers),
        status=status.value,
        latency_ms=12,
        detail=detail,
    )


def _runner(statuses: dict[str, ProbeStatus], *, slow: str | None = None):
    def run(probe, config):
        if probe.key == slow:
            time.sleep(1.0)
        return _result(probe, statuses.get(probe.key, ProbeStatus.OK), "detail")

    return run


def test_informational_sources_never_stop_the_init(tmp_path):
    cfg = Config(data_root=tmp_path / "data")
    down = {key: ProbeStatus.DOWN for key in preflight.INFORMATIONAL_PROBES}
    outcome = preflight.run_init_preflight(cfg, probe_runner=_runner(down))

    assert [r.key for r in outcome.results][0] == "tdx_protocol"
    assert outcome.ok


def test_an_unreachable_tdx_fails_the_preflight(tmp_path):
    cfg = Config(data_root=tmp_path / "data")
    outcome = preflight.run_init_preflight(
        cfg, probe_runner=_runner({"tdx_protocol": ProbeStatus.DOWN})
    )

    assert not outcome.ok
    assert [r.key for r in outcome.failed_required] == ["tdx_protocol"]


def test_a_tdx_turned_off_in_the_config_is_not_a_failure(tmp_path):
    cfg = Config(data_root=tmp_path / "data")
    outcome = preflight.run_init_preflight(
        cfg, probe_runner=_runner({"tdx_protocol": ProbeStatus.SKIPPED})
    )
    assert outcome.ok


def test_a_probe_past_the_budget_counts_as_down(tmp_path):
    cfg = Config(data_root=tmp_path / "data")
    started = time.monotonic()
    outcome = preflight.run_init_preflight(
        cfg, budget=0.1, probe_runner=_runner({}, slow="tdx_protocol")
    )

    assert time.monotonic() - started < 0.9, "the check must not wait for a slow probe"
    tdx = outcome.results[0]
    assert tdx.status == ProbeStatus.DOWN.value
    assert "no response" in tdx.detail
    assert not outcome.ok


def test_the_preflight_is_skipped_offline_and_under_the_test_guard(tmp_path, monkeypatch):
    cfg = Config(data_root=tmp_path / "data")
    # The suite's own network guard is reason enough on its own.
    assert preflight.preflight_skip_reason(cfg) == "test network guard"

    monkeypatch.delenv("CNE_TEST_NO_NETWORK", raising=False)
    monkeypatch.delenv(preflight.SKIP_ENV, raising=False)
    assert preflight.preflight_skip_reason(cfg) is None
    assert preflight.preflight_skip_reason(cfg, requested_skip=True) == "--no-preflight"
    monkeypatch.setenv(preflight.SKIP_ENV, "1")
    assert preflight.preflight_skip_reason(cfg) == preflight.SKIP_ENV
    monkeypatch.delenv(preflight.SKIP_ENV)
    assert preflight.preflight_skip_reason(Config(data_root=tmp_path, tdx_allow_mock=True))


def _config(tmp_path) -> str:
    path = tmp_path / "cnequity.toml"
    path.write_text(
        f'[data]\nroot = "{path_for_toml(tmp_path / "data")}"\n\n'
        '[job.init.phases]\nnames = ["phase1_reference"]\n'
    )
    return str(path)


@pytest.mark.usefixtures("chinese_output")
def test_init_stops_in_seconds_when_tdx_is_unreachable(tmp_path, monkeypatch):
    started: list[bool] = []
    monkeypatch.setattr(preflight, "preflight_skip_reason", lambda config, requested_skip: None)
    monkeypatch.setattr(
        preflight,
        "run_init_preflight",
        lambda config: preflight.PreflightResult(
            results=[
                _result(preflight.PROBES_BY_KEY["tdx_protocol"], ProbeStatus.DOWN, "ConnectTimeout")
            ],
            required=preflight.REQUIRED_PROBES,
        ),
    )
    monkeypatch.setattr(
        JobEngine, "run_init_phases", lambda self, trade_date=None, **kw: started.append(True)
    )

    result = CliRunner().invoke(cli, ["init", "--config", _config(tmp_path)])

    assert result.exit_code == 1
    assert "连不上" in result.output
    assert "--no-preflight" in result.output
    assert started == []


@pytest.mark.usefixtures("chinese_output")
def test_init_goes_on_when_only_informational_sources_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "preflight_skip_reason", lambda config, requested_skip: None)
    statuses = {"exchange_sse": ProbeStatus.BLOCKED}
    monkeypatch.setattr(
        preflight,
        "run_init_preflight",
        lambda config: preflight.PreflightResult(
            results=[
                _result(preflight.PROBES_BY_KEY[key], statuses.get(key, ProbeStatus.OK))
                for key in (*preflight.REQUIRED_PROBES, *preflight.INFORMATIONAL_PROBES)
            ],
            required=preflight.REQUIRED_PROBES,
        ),
    )
    monkeypatch.setattr(
        JobEngine,
        "run_init_phases",
        lambda self, trade_date=None, **kw: {"run_id": "r", "status": "success", "phases": []},
    )

    result = CliRunner().invoke(cli, ["init", "--config", _config(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "网络预检" in result.output
    assert "不挡住建湖" in result.output
