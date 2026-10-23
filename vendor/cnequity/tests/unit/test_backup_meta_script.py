import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform == "win32", reason="backup_meta.sh is a Unix shell script")
def test_backup_meta_accepts_relative_root_and_preserves_evidence(tmp_path):
    data_root = tmp_path / "lake"
    meta_root = data_root / "meta"
    meta_root.mkdir(parents=True)
    with sqlite3.connect(meta_root / "manifest.db") as connection:
        connection.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY)")

    evidence_dirs = {
        "state",
        "quality",
        "revisions",
        "source_snapshots",
        "source_health",
        "stability",
    }
    for name in evidence_dirs:
        directory = meta_root / name
        directory.mkdir()
        (directory / "evidence.json").write_text("{}", encoding="utf-8")

    backup_dir = tmp_path / "backup"
    script = Path(__file__).resolve().parents[2] / "scripts" / "scheduler" / "backup_meta.sh"
    subprocess.run(
        [str(script), "lake", "backup", "30"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    archives = list(backup_dir.glob("meta-*.tar.gz"))
    assert len(archives) == 1
    with tarfile.open(archives[0], "r:gz") as archive:
        names = set(archive.getnames())

    assert "manifest.db" in names
    for name in evidence_dirs:
        assert f"{name}/evidence.json" in names
