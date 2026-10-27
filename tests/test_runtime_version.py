"""Installer version checks reject stale editable metadata without editing it."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest


SPEC = importlib.util.spec_from_file_location("lake_installer_version", Path(__file__).parents[1] / "install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def source_tree(root, package="0.2.0", release=None):
    (root / "pyproject.toml").write_text('[project]\nname="lake-equity"\nversion="0.2.0"\n', encoding="utf-8")
    package_root = root / "src/lake_equity"
    package_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text(f'__version__ = "{package}"\n', encoding="utf-8")
    if release:
        (root / "release-manifest.json").write_text(json.dumps({"version": release}), encoding="utf-8")


def test_source_and_release_versions_must_agree_before_installation(tmp_path):
    source_tree(tmp_path, release="0.2.0")
    assert installer.source_version(tmp_path) == "0.2.0"
    (tmp_path / "release-manifest.json").write_text('{"version":"0.1.0"}')
    with pytest.raises(ValueError, match="Release manifest version"):
        installer.source_version(tmp_path)
    (tmp_path / "release-manifest.json").unlink()
    (tmp_path / "src/lake_equity/__init__.py").write_text('__version__="0.1.0"')
    with pytest.raises(ValueError, match="Source package version"):
        installer.source_version(tmp_path)


@pytest.mark.parametrize("installed,package,passes", [("0.2.0", "0.2.0", True), ("0.1.0", "0.2.0", False), ("0.2.0", "0.1.0", False)])
def test_target_interpreter_checks_installed_metadata_and_imported_package(tmp_path, monkeypatch, installed, package, passes):
    source_tree(tmp_path, package=package)
    metadata = tmp_path / "src/lake_equity-test.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(f"Metadata-Version: 2.4\nName: lake-equity\nVersion: {installed}\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "src"))
    before = (metadata / "METADATA").read_bytes()
    if passes:
        observed = installer.verify_runtime_version(Path(sys.executable), tmp_path, "0.2.0")
        assert observed == {"expected": "0.2.0", "installed": installed, "package": package}
    else:
        with pytest.raises(ValueError, match="Installed runtime version mismatch"):
            installer.verify_runtime_version(Path(sys.executable), tmp_path, "0.2.0")
    assert (metadata / "METADATA").read_bytes() == before


def test_runtime_check_uses_entrypoint_source_path_and_rejects_shadow_metadata(tmp_path, monkeypatch):
    source_tree(tmp_path)
    stale = tmp_path / 'src/lake_equity.egg-info'
    stale.mkdir()
    metadata = stale / 'PKG-INFO'
    metadata.write_text('Metadata-Version: 2.4\nName: lake-equity\nVersion: 0.1.0\n', encoding='utf-8')
    monkeypatch.delenv('PYTHONPATH', raising=False)
    before = metadata.read_bytes()
    with pytest.raises(ValueError, match='Installed runtime version mismatch'):
        installer.verify_runtime_version(Path(sys.executable), tmp_path, '0.2.0')
    assert metadata.read_bytes() == before
