from __future__ import annotations

import json
from pathlib import Path

from lake_equity.update_progress import UpdateProgress


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


def append(path: Path, text: str):
    with path.open("ab") as handle:
        handle.write(text.encode("utf-8"))


def counter(root: Path, requests: int, responses: int):
    root.mkdir(parents=True, exist_ok=True)
    (root / "meter-sina.json").write_text(
        json.dumps({"policy_day": "2026-10-26", "aliases": {"sina_bars": requests}, "total": requests}),
        encoding="utf-8")
    (root / "wire-sina.json").write_text(
        json.dumps({"policy_day": "2026-10-26", "responses": responses}), encoding="utf-8")


def test_only_new_business_output_resets_idle_not_heartbeat(tmp_path):
    clock = Clock()
    log = tmp_path / "daily.log"
    log.write_text("INFO: Step daily_bars starting\n", encoding="utf-8")
    monitor = UpdateProgress(tmp_path, log, log.stat().st_size, clock=clock)
    clock.value = 301
    append(log, "INFO: still working: daily_bars 5m (no output for 1m00s)\n")
    shown = monitor.sample()
    assert shown["elapsed_seconds"] == shown["idle_seconds"] == 301
    assert "无" in shown["warning"]
    assert "daily_bars" not in shown["message"]  # history belongs to another run

    append(log, "INFO engine: Step index_bars starting\n")
    shown = monitor.sample()
    assert shown["idle_seconds"] == 0
    assert shown["warning"] is None
    assert "index_bars" in shown["message"]


def test_sina_activity_is_source_count_not_inherited_batch_percentage(tmp_path):
    clock = Clock()
    log = tmp_path / "daily.log"
    log.touch()
    rate = tmp_path / "var/lake/meta/rate_limits"
    counter(rate, 1000, 995)
    monitor = UpdateProgress(tmp_path, log, 0, clock=clock)
    append(log, "INFO engine: Step daily_bars starting\n")
    append(log, "INFO worker_pool: daily_bars 53/53 batches (24/24 symbols failed) · 0 rows\n")
    shown = monitor.sample()
    assert (shown["current"], shown["total"]) == (53, 53)
    assert shown["progress_scope"] == "current_stage"

    clock.value = 10
    counter(rate, 1010, 1004)
    shown = monitor.sample()
    assert "新浪备用源" in shown["message"]
    assert "10 次请求" in shown["message"] and "9 次响应" in shown["message"]
    assert "源级计数" in shown["message"]
    assert shown["current"] is None and shown["total"] is None
    assert shown["idle_seconds"] == 0

    clock.value = 311
    append(log, "INFO: still working: daily_bars 5m (no output for 1m00s)\n")
    assert "无" in monitor.sample()["warning"]
    counter(rate, 1011, 1005)
    assert monitor.sample()["warning"] is None


def test_counter_activity_outside_daily_step_does_not_mask_stall(tmp_path):
    clock = Clock()
    log = tmp_path / "daily.log"
    log.touch()
    rate = tmp_path / "var/lake/meta/rate_limits"
    counter(rate, 2, 2)
    monitor = UpdateProgress(tmp_path, log, 0, clock=clock)
    append(log, "INFO engine: Step compact starting\n")
    monitor.sample()
    clock.value = 400
    counter(rate, 200, 190)
    shown = monitor.sample()
    assert "新浪" not in shown["message"]
    assert shown["idle_seconds"] == 400
    assert shown["warning"]


def test_new_step_does_not_keep_an_unrelated_source_warning(tmp_path):
    log = tmp_path / "daily.log"
    log.touch()
    monitor = UpdateProgress(tmp_path, log, 0)
    append(log, "INFO: Step index_bars starting\nWARNING: TDX fetch failed\n")
    assert "通达信" in monitor.sample()["warning"]
    append(log, "INFO: Step announcement_index starting\n")
    shown = monitor.sample()
    assert "announcement_index" in shown["message"]
    assert shown["warning"] is None


def test_initially_unreadable_counters_establish_baseline_without_fake_growth(tmp_path):
    clock = Clock()
    log = tmp_path / "daily.log"
    log.touch()
    rate = tmp_path / "var/lake/meta/rate_limits"
    rate.mkdir(parents=True)
    (rate / "meter-sina.json").write_text("{bad", encoding="utf-8")
    (rate / "wire-sina.json").write_text("{bad", encoding="utf-8")
    monitor = UpdateProgress(tmp_path, log, 0, clock=clock)
    append(log, "INFO: Step daily_bars starting\n")
    monitor.sample()

    clock.value = 10
    counter(rate, 9000, 8998)
    shown = monitor.sample()
    assert "9000" not in shown["message"]
    assert "新浪备用源" not in shown["message"]
    assert shown["idle_seconds"] == 10

    counter(rate, 9001, 8999)
    shown = monitor.sample()
    assert "1 次请求、1 次响应" in shown["message"]
    assert shown["idle_seconds"] == 0


def test_completed_daily_step_does_not_keep_sina_message_or_total(tmp_path):
    clock = Clock()
    log = tmp_path / "daily.log"
    log.touch()
    rate = tmp_path / "var/lake/meta/rate_limits"
    counter(rate, 0, 0)
    monitor = UpdateProgress(tmp_path, log, 0, clock=clock)
    append(log, "INFO: Step daily_bars starting\n")
    monitor.sample()
    counter(rate, 20, 19)
    assert "新浪" in monitor.sample()["message"]
    append(log, "INFO: Step daily_bars warning in 120s (5239 rows)\n")
    append(log, "INFO: Step sector_bars starting\n")
    append(log, "INFO: THS sweep: 80/432 boards (ok=80 failed=0)\n")
    shown = monitor.sample()
    assert "新浪" not in shown["message"]
    assert (shown["current"], shown["total"]) == (80, 432)


def test_custom_root_broken_counters_and_safe_failure_messages(tmp_path):
    clock = Clock()
    log = tmp_path / "daily.log"
    log.touch()
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/cnequity.toml").write_text('[data]\nroot = "custom-lake"\n', encoding="utf-8")
    rate = tmp_path / "custom-lake/meta/rate_limits"
    counter(rate, 50, 49)
    monitor = UpdateProgress(tmp_path, log, 0, clock=clock)
    append(log, "INFO engine: Step daily_bars starting\n")
    append(log, "WARNING worker: daily_bars batch abc failed: TDX fetch failed: private-url?token=secret\n")
    shown = monitor.sample()
    assert "通达信" in shown["warning"]
    assert "secret" not in str(shown)
    counter(rate, 51, 50)
    assert "1 次请求" in monitor.sample()["message"]
    (rate / "meter-sina.json").write_text("{bad", encoding="utf-8")
    assert "1 次请求" in monitor.sample()["message"]


def test_partial_utf8_and_oversized_log_lines_stay_bounded(tmp_path):
    clock = Clock()
    log = tmp_path / "daily.log"
    log.touch()
    monitor = UpdateProgress(tmp_path, log, 0, clock=clock)
    append(log, "x" * 200_000)
    for _ in range(5):
        monitor.sample()
    append(log, "\nINFO: Step daily_bars starting\n")
    payload = "WARNING: sina bars failed for 000860.SZ: 超时\n".encode("utf-8")
    with log.open("ab") as handle:
        handle.write(payload[:-2])
    monitor.sample()
    with log.open("ab") as handle:
        handle.write(payload[-2:])
    shown = monitor.sample()
    assert "新浪" in shown["message"]
    assert "超时" in shown["warning"]
    assert len(shown["message"]) < 256

