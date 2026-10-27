"""Workbench shell contracts: frontend version identity and preview validation."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from lake_equity.app import _frontend_version
from lake_equity.learning.preview import BacktestPreview


def _touch(path: Path, content: str) -> None:
    previous = path.stat().st_mtime_ns if path.exists() else 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    stamp = max(previous, path.stat().st_mtime_ns) + 1_000_000
    os.utime(path, ns=(stamp, stamp))


def test_frontend_version_tracks_module_content_not_vendor(tmp_path):
    _touch(tmp_path / "frontend/app.js", "console.log(1);")
    _touch(tmp_path / "frontend/styles.css", "body{color:red}")
    _touch(tmp_path / "frontend/vendor/echarts.min.js", "vendor-one")
    first = _frontend_version(tmp_path)
    assert len(first) == 16 and all(character in "0123456789abcdef" for character in first)
    _touch(tmp_path / "frontend/vendor/echarts.min.js", "vendor-two")
    assert _frontend_version(tmp_path) == first
    _touch(tmp_path / "frontend/app.js", "console.log(2);")
    assert _frontend_version(tmp_path) != first
    current = _frontend_version(tmp_path)
    (tmp_path / "frontend/README.md").write_text("ignored", encoding="utf-8")
    assert _frontend_version(tmp_path) == current


def test_frontend_version_detects_same_size_same_timestamp_content_change(tmp_path):
    target = tmp_path / "frontend/app.js"
    _touch(target, "console.log(1);")
    before = target.stat()
    first = _frontend_version(tmp_path)
    target.write_text("console.log(2);", encoding="utf-8")
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert target.stat().st_size == before.st_size
    assert target.stat().st_mtime_ns == before.st_mtime_ns
    assert _frontend_version(tmp_path) != first


def test_frontend_version_is_stable_across_timestamp_only_changes(tmp_path):
    target = tmp_path / "frontend/app.js"
    _touch(target, "console.log(1);")
    first = _frontend_version(tmp_path)
    _touch(target, "console.log(1);")
    assert _frontend_version(tmp_path) == first


def test_preview_rejects_invalid_requests_before_touching_data(tmp_path):
    preview = BacktestPreview(tmp_path)
    cases = [
        ({"mode": "unknown"}, "Unknown preview mode"),
        ({"mode": "frozen", "scope": "everything"}, "scope must be"),
        ({"mode": "frozen", "scope": "symbols", "symbols": ["600519.SH"], "start": "2026-10-22", "end": "2026-10-24", "universe_scope": "magic"}, "universe_scope must be"),
        ({"mode": "frozen", "scope": "all", "symbols": [], "start": "2026-10-22", "end": "2026-10-24", "universe_scope": "explicit_symbols"}, "requires an explicit"),
        ({"mode": "frozen", "scope": "symbols", "symbols": [], "start": "2026-10-22", "end": "2026-10-24"}, "at least one symbol"),
        ({"mode": "frozen", "scope": "symbols", "symbols": ["830799.BJ"], "start": "2026-10-22", "end": "2026-10-24"}, "Unsupported symbol"),
        ({"mode": "frozen", "scope": "symbols", "symbols": ["600519.SH"], "start": "2026-10-24", "end": "2026-10-22"}, "must not exceed"),
        ({"mode": "frozen", "scope": "symbols", "symbols": ["600519.SH"], "start": "2026-1-1", "end": "2026-10-24"}, "ISO date"),
        ({"mode": "frozen", "scope": "symbols", "symbols": ["600519.SH"], "start": "2026-10-22", "end": "2026-10-24", "extra": 1}, "Unknown preview parameters"),
        ({"mode": "frozen", "scope": "symbols", "symbols": [f"{index:06d}.SZ" for index in range(201)], "start": "2026-10-22", "end": "2026-10-24"}, "at most 200"),
    ]
    for params, match in cases:
        with pytest.raises(ValueError, match=match):
            preview.preview(params)
    assert not (tmp_path / "var").exists()


def test_quarantine_routes_are_read_only_without_a_workspace(tmp_path):
    from lake_equity.learning.quarantine import QuarantineService

    listing = QuarantineService(tmp_path).listing()
    assert listing["plan_id"].startswith("qplan-")
    assert listing["items"] == [] and listing["total_bytes"] == 0
    assert not (tmp_path / "var/learning/workflows").is_dir() or not any((tmp_path / "var/learning/workflows").iterdir())
