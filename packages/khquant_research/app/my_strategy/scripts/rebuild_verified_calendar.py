"""Rebuild the verified trading calendar run from two independent sources.

``calendar_bootstrap_*`` directories under ``artifacts/runs/`` are protected
runtime infrastructure: analysis and training load them through
``load_verified_calendar()`` and fail site-wide when they are missing. They
are NOT deletable run records. If one is lost (for example by a run-record
cleanup), this script rebuilds it exactly to the loader contract:

- two independent sources are cross-checked for equality inside the requested
  range: the akshare installed exchange calendar and the sina ``klc_td_sh``
  schedule (decoded like akshare's own trade-date tool);
- both source artifacts are frozen under ``sources/`` with sha256 stamps;
- ``reports/calendar.json`` carries verified/source/dates plus the observed
  market data end, and ``infrastructure.json`` marks the directory as
  protected so future cleanups can recognize it.

Usage (from ``app/my_strategy`` with the venv python)::

    python -I scripts/rebuild_verified_calendar.py
    python -I scripts/rebuild_verified_calendar.py --run-id calendar_bootstrap_20261018
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from my_strategy.core.paths import ARTIFACT_RUNS_ROOT
from my_strategy.core.tz import local_now

DEFAULT_RUN_ID = "calendar_bootstrap_20261018"
DEFAULT_START = "2015-01-01"
DEFAULT_END = "2026-12-31"
SINA_URL = "https://finance.sina.com.cn/realstock/company/klc_td_sh.txt"
CALENDAR_SOURCE = "akshare_installed_calendar_x_sina_klc_td_sh"


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _installed_akshare_calendar():
    import akshare
    return Path(akshare.__file__).resolve().parent / "file_fold" / "calendar.json"


def load_akshare_sessions(path=None):
    """Sessions from the akshare installed exchange calendar (no network)."""
    source = Path(path) if path else _installed_akshare_calendar()
    import pandas as pd
    return sorted({pd.Timestamp(item).date().isoformat()
                   for item in json.loads(source.read_text(encoding="utf-8"))}), source


def fetch_sina_sessions(url=SINA_URL, timeout=30):
    """Sessions from the sina klc_td_sh schedule, decoded like akshare does."""
    import pandas as pd
    import requests
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    from akshare.tool.trade_date_hist import hk_js_decode, py_mini_racer
    decoder = py_mini_racer.MiniRacer()
    try:
        decoder.eval(hk_js_decode)
        text = response.content.decode("utf-8")
        decoded = decoder.call("d", text.split("=", 1)[1].split(";", 1)[0].replace('"', ""))
    finally:
        decoder.close()
    return sorted({pd.Timestamp(item).date().isoformat() for item in decoded}), response.content


def rebuild_calendar_run(runs_root, run_id, *, start=DEFAULT_START, end=DEFAULT_END,
                         akshare_sessions=None, akshare_artifact=None,
                         sina_sessions=None, sina_artifact_bytes=None,
                         observed_end=None):
    """Write the calendar run and validate it against the loader contract.

    ``akshare_sessions``/``sina_sessions`` are injectable for tests; the real
    sources are loaded/fetched by ``main``. The two independent schedules must
    agree exactly inside [start, end]; a mismatch refuses to write anything.
    """
    if not akshare_sessions or not sina_sessions:
        raise ValueError("两个独立日历来源都必须提供")
    runs_root = Path(runs_root)
    ak_range = [day for day in akshare_sessions if start <= day <= end]
    sina_range = [day for day in sina_sessions if start <= day <= end]
    if ak_range != sina_range:
        only_ak = sorted(set(ak_range) - set(sina_range))[:5]
        only_sina = sorted(set(sina_range) - set(ak_range))[:5]
        raise ValueError(f"独立日历来源不一致：akshare-only={only_ak} sina-only={only_sina}")
    dates = ak_range
    if not dates:
        raise ValueError(f"[{start}, {end}] 内没有已核验交易日")

    root = runs_root / run_id
    sources_dir = root / "sources"
    reports_dir = root / "reports"
    sources_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    ak_target = sources_dir / "installed_akshare_calendar.json"
    if akshare_artifact is not None:
        shutil.copyfile(akshare_artifact, ak_target)
    else:
        ak_target.write_text(json.dumps(akshare_sessions), encoding="utf-8")
    sina_target = sources_dir / "sina_klc_td_sh.txt"
    sina_target.write_bytes(sina_artifact_bytes if sina_artifact_bytes is not None
                            else json.dumps(sina_sessions).encode("utf-8"))

    now = local_now().isoformat()
    calendar = {
        "run_id": run_id,
        "verified": True,
        "source": CALENDAR_SOURCE,
        "independent_from_market_bars": True,
        "start": dates[0],
        "end": dates[-1],
        "dates": dates,
        "source_artifacts": [
            {"path": f"runs/{run_id}/sources/installed_akshare_calendar.json", "sha256": _sha(ak_target)},
            {"path": f"runs/{run_id}/sources/sina_klc_td_sh.txt", "sha256": _sha(sina_target)},
        ],
        "observed_market_data_end": observed_end,
        "executable_verified_range_end": observed_end,
        "schedule_verified_range_end": dates[-1],
        "recreated_at": now,
        "recreation_reason": "rebuilt from independent sources by scripts/rebuild_verified_calendar.py",
    }
    (reports_dir / "calendar.json").write_text(
        json.dumps(calendar, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "metadata.json").write_text(json.dumps({
        "run_id": run_id, "kind": "calendar", "status": "completed", "created_at": now,
        "calendar": {"verified": True, "sessions": len(dates), "first": dates[0], "last": dates[-1]},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "infrastructure.json").write_text(json.dumps({
        "infrastructure": True, "kind": "verified_calendar", "run_id": run_id,
        "note": "受保护的运行时基础设施：分析与训练依赖此日历；运行记录清理不得删除。"
                "丢失时用 scripts/rebuild_verified_calendar.py 从独立来源重建。",
        "created_at": now,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    from my_strategy.services.czsc_research_calendar import load_verified_calendar
    loaded_dates, meta = load_verified_calendar(run_id, runs_root=runs_root, require_sources=True)
    if loaded_dates != dates:
        raise RuntimeError("重建后的日历未通过加载器校验")
    return meta


def main(argv=None):
    parser = argparse.ArgumentParser(description="从独立来源重建已验证交易日历运行目录")
    parser.add_argument("--runs-root", default=str(ARTIFACT_RUNS_ROOT))
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID)
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--observed-end", default=None,
                        help="观测到的最新行情日；缺省从原始行情库读取")
    args = parser.parse_args(argv)

    akshare_sessions, ak_artifact = load_akshare_sessions()
    sina_sessions, sina_bytes = fetch_sina_sessions()
    observed_end = args.observed_end
    if observed_end is None:
        from my_strategy.adapters.czsc_adapter import latest_market_date
        observed_end = str(latest_market_date())
    meta = rebuild_calendar_run(
        args.runs_root, args.run_id, start=args.start, end=args.end,
        akshare_sessions=akshare_sessions, akshare_artifact=ak_artifact,
        sina_sessions=sina_sessions, sina_artifact_bytes=sina_bytes,
        observed_end=observed_end)
    print(f"已重建并校验 {args.run_id}：{meta['session_count']} 个交易日 "
          f"{meta['first_session']}..{meta['last_session']}（观测行情截至 {observed_end}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
