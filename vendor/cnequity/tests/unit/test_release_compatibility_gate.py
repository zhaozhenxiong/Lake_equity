import copy
import runpy
import subprocess
from pathlib import Path

from cnequity.domain.contracts import export_contract

GATE = runpy.run_path(
    str(Path(__file__).resolve().parents[2] / "scripts/dev/check_release_contract.py")
)


def test_previous_release_excludes_current_and_prerelease_tags(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "test",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    for tag in ["v0.9.0", "v0.11.0", "v0.12.0", "v0.13.0rc1"]:
        subprocess.run(["git", "tag", tag], cwd=tmp_path, check=True)
    assert GATE["previous_release"](tmp_path, "0.12.0.dev0") == "v0.11.0"


def test_breaking_changes_need_both_schema_bump_and_migration(tmp_path):
    old = export_contract()
    new = copy.deepcopy(old)
    new["datasets"]["daily_bars"]["unit_contract"] = {"price": "changed"}
    errors = GATE["compatibility_errors"](old, new, tmp_path)
    assert any("schema_version" in error for error in errors)
    assert any("Migration" in error for error in errors)
    new["datasets"]["daily_bars"]["schema_version"] += 1
    (tmp_path / "daily_bars.md").write_text(
        "## Change\nPrice unit changed.\n## Migration\nRescale old prices.\n## Rollback\nRestore previous snapshot.\n"
    )
    assert GATE["compatibility_errors"](old, new, tmp_path) == []


def test_unchanged_contract_does_not_require_migration(tmp_path):
    contract = export_contract()
    assert GATE["compatibility_errors"](contract, contract, tmp_path) == []
