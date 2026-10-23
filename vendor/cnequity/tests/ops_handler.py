"""Stand-in for the Click command a dashboard job would run.

Selected with ``CNE_OPS_HANDLER=ops_handler:main``. The argv is still the one
the preview built; this only decides how that process ends.
"""

from __future__ import annotations

import json
import os
import sys
import time


def main(argv: list[str]) -> int:
    mode = os.environ.get("CNE_OPS_STUB_MODE", "ok")
    if mode == "unicode":
        print(
            json.dumps({"status": "success", "message": "取数完成：中文"}, ensure_ascii=False),
            flush=True,
        )
        print("进度：中文日志", file=sys.stderr, flush=True)
        return 0
    if mode == "sleep":
        time.sleep(30)
        return 0
    if mode == "click":
        import click

        raise click.ClickException("锁被占用")
    if mode == "boom":
        raise RuntimeError("boom")
    if mode == "gate":
        print("not ok", flush=True)
        return 1
    if mode == "fail":
        print(json.dumps({"status": "failed"}), flush=True)
        return 1
    if mode == "partial":
        from cnequity.config import load_config
        from cnequity.orchestrator.manifest import Manifest

        config = load_config(argv[argv.index("--config") + 1])
        manifest = Manifest(config.manifest_path)
        run_id = manifest.start_run("daily:core", {"trade_date": "2026-01-02"})
        manifest.finish_run(run_id, "warning")
        print(json.dumps({"status": "warning", "run_id": run_id}), flush=True)
        return 0
    print(json.dumps({"status": "success"}), flush=True)
    return 0
