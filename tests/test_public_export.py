"""Synthetic release trees prove privacy boundaries and reproducible archives."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import zipfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/prepare_release.py"
PRIVATE = b"SYNTHETIC_PRIVATE_VALUE_MUST_NEVER_BE_DISTRIBUTED"


def load_exporter(path):
    spec = importlib.util.spec_from_file_location("synthetic_public_export", path)
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


@pytest.fixture
def exporter():
    return load_exporter(SCRIPT)


def write(root, relative, content):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    return path


def snapshot(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


@pytest.fixture
def source_tree(tmp_path, exporter):
    root = tmp_path / "synthetic-source"
    expected = set()

    def public(relative, content="synthetic public source\n"):
        write(root, relative, content)
        expected.add(relative)

    for relative in exporter.ROOT_FILES:
        public(relative, '[project]\nname = "lake-equity"\nversion = "0.1.0"\n' if relative == "pyproject.toml" else "synthetic public source\n")
    for name in exporter.PUBLIC_DOCS:
        public(f"docs/{name}")
    for name in exporter.PUBLIC_SCRIPTS:
        public(f"scripts/{name}", SCRIPT.read_bytes() if name == "prepare_release.py" else "# synthetic script\n")
    for name in exporter.PUBLIC_ASSETS:
        public(f"docs/assets/{name}")
    for name in ("chart_assets.json", "learning_runtime.json"):
        public(f"docs/provenance/{name}", '{"synthetic_public_metadata": true}\n')
    for relative in ("src/lake_equity/__init__.py", "src/lake_equity/data/__init__.py",
                     "frontend/index.html", "frontend/vendor/LICENSE.synthetic",
                     "tests/test_synthetic.py", "third_party/synthetic/LICENSE"):
        public(relative, '__version__ = "0.1.0"\n' if relative == "src/lake_equity/__init__.py" else "synthetic public source\n")

    frozen = [
        ("cnequity_source.json", "vendor/cnequity", [
            "pyproject.toml", "LICENSE", "src/cnequity/__init__.py",
            "src/cnequity/config/templates/cnequity.example.toml", "sources/SOURCES.yml",
            "contracts/v0.18.0.json"]),
        ("khquant_source.json", "packages/khquant_research/app", [
            "my_strategy/__init__.py", "my_strategy/configs/cost/flat_v1.yaml",
            "vendor/czsc/crates/czsc-utils/data/synthetic.csv", "vendor/czsc/LICENSE",
            "vendor/kronos/model.py", "vendor/wheels/wheel-index.json"]),
    ]
    for name, base, paths in frozen:
        entries = []
        for relative in paths:
            path = f"{base}/{relative}"
            public(path)
            entries.append(dict(path=relative, sha256=exporter.sha256(root / path)))
        if name == "khquant_source.json":
            # Explicit manifest entries can still be omitted from public output.
            for relative in ("my_strategy/docs/history.md", "my_strategy/data/account.json",
                             "my_strategy/knowledge_base_system/audit.json", "AI_CHANGELOG.md"):
                path = write(root, f"{base}/{relative}", PRIVATE)
                entries.append(dict(path=relative, sha256=exporter.sha256(path)))
            # The old manifest also permits a package-root LICENSE fallback.
            path = "packages/khquant_research/LICENSE"
            public(path)
            entries.append(dict(path="LICENSE", sha256=exporter.sha256(root / path)))
        manifest = dict(version="synthetic", git_head="a" * 40, dirty=True,
                        archive_sha256="b" * 64, files=entries,
                        archive=PRIVATE.decode(), worktree_status=PRIVATE.decode())
        write(root, f"docs/provenance/{name}", json.dumps(manifest))

    for relative in (
        "var/learning/models/private.npz", "var/logs/private.log", "configs/cnequity.toml",
        "docs/PRIVATE.md", "docs/provenance/state_migration.json", ".env", ".env.local",
        "private-audit.json", "docs/assets/private.png", "src/.env", "src/lake_equity/__pycache__/private.pyc",
        "frontend/node_modules/private.js", "tests/.pytest_cache/private.json",
        "src/lake_equity.egg-info/PKG-INFO", "src/lake_equity-0.0.1.dist-info/METADATA",
        "tests/test_fill_lake.py", "tests/test_fill_lake_years.py", "tests/test_repair_calendar.py",
        "packages/khquant_research/app/my_strategy/unlisted-audit.json",
        "packages/khquant_research/app/my_strategy/configs/unlisted-private.yaml",
        "vendor/cnequity/unlisted-private.json",
    ):
        write(root, relative, PRIVATE)
    return root, expected


def test_export_distributes_only_the_public_subset_and_preserves_source_data_resources(
        tmp_path, exporter, source_tree):
    root, expected = source_tree
    before = snapshot(root)
    result = exporter.build_release(root, tmp_path / "export", "0.1.0")
    repository = Path(result["repository"])
    expected |= {"docs/provenance/sources.json", "release-manifest.json"}
    assert {path.relative_to(repository).as_posix() for path in repository.rglob("*")
            if path.is_file()} == expected
    assert "src/lake_equity/data/__init__.py" in expected
    assert "scripts/validate_browser.mjs" in expected
    assert "packages/khquant_research/app/vendor/czsc/crates/czsc-utils/data/synthetic.csv" in expected
    assert snapshot(root) == before
    manifest = json.loads((repository / "release-manifest.json").read_text())
    assert manifest["schema"] == 1 and manifest["kind"] == "lake-equity-public-source"
    assert {entry["path"] for entry in manifest["files"]} == expected - {"release-manifest.json"}
    for entry in manifest["files"]:
        path = repository / entry["path"]
        assert path.stat().st_size == entry["size"]
        assert exporter.sha256(path) == entry["sha256"]
    with zipfile.ZipFile(result["archive"]) as bundle:
        assert {name.removeprefix("LakeEquity-v0.1.0/") for name in bundle.namelist()} == expected
        assert all(PRIVATE not in bundle.read(name) for name in bundle.namelist())
    assert (tmp_path / "export/SHA256SUMS.txt").read_text().split()[0] == result["sha256"]


def test_reviewed_omission_is_verified_and_rebuilt_from_public_output(tmp_path, exporter, source_tree):
    root, expected = source_tree
    relative = "vendor/cnequity/contracts/v0.18.0.json"
    original = root / relative
    omission = dict(source_manifest="cnequity_source.json", path="contracts/v0.18.0.json",
                    original_sha256=exporter.sha256(original), reason="Synthetic reviewed omission")
    original.unlink()
    write(root, "docs/provenance/local_omissions.json", json.dumps(dict(schema=1, omissions=[omission])))
    expected.remove(relative)
    expected.add("docs/provenance/local_omissions.json")
    first = exporter.build_release(root, tmp_path / "export-one", "0.1.0")
    repository = Path(first["repository"])
    assert not (repository / relative).exists()
    sources = json.loads((repository / "docs/provenance/sources.json").read_text())
    cnequity = next(source for source in sources["sources"] if source["source"] == "cnequity")
    assert cnequity["omitted_file_count"] == 1
    assert sources["local_omissions"] == [omission]
    second = exporter.build_release(repository, tmp_path / "export-two", "0.1.0")
    assert second["sha256"] == first["sha256"]
    assert snapshot(Path(second["repository"])) == snapshot(repository)


@pytest.mark.parametrize("change", ["undeclared", "wrong_hash", "still_present"])
def test_omission_does_not_hide_unreviewed_or_stale_source_changes(tmp_path, exporter, source_tree, change):
    root, _ = source_tree
    source = root / "vendor/cnequity/contracts/v0.18.0.json"
    original_sha256 = exporter.sha256(source)
    if change != "still_present":
        source.unlink()
    if change != "undeclared":
        omission = dict(source_manifest="cnequity_source.json", path="contracts/v0.18.0.json",
                        original_sha256="0" * 64 if change == "wrong_hash" else original_sha256,
                        reason="Synthetic reviewed omission")
        write(root, "docs/provenance/local_omissions.json", json.dumps(dict(schema=1, omissions=[omission])))
    with pytest.raises(ValueError, match="Frozen input|omission"):
        exporter.build_release(root, tmp_path / "must-not-exist", "0.1.0")
    assert not (tmp_path / "must-not-exist").exists()


def test_existing_output_is_never_overwritten(tmp_path, exporter, source_tree):
    root, _ = source_tree
    output = tmp_path / "existing"
    write(output, "preserve.txt", b"existing user bytes")
    before = snapshot(output)
    with pytest.raises(FileExistsError):
        exporter.build_release(root, output, "0.1.0")
    assert snapshot(output) == before


@pytest.mark.parametrize("mismatch", ["requested", "package"])
def test_release_rejects_version_drift_before_writing_output(tmp_path, exporter, source_tree, mismatch):
    root, expected = source_tree
    if mismatch == "package":
        write(root, "src/lake_equity/__init__.py", '__version__="0.0.1"\n')
    output = tmp_path / "version-mismatch"
    with pytest.raises(ValueError, match="versions must match"):
        exporter.build_release(root, output, "0.2.0" if mismatch == "requested" else "0.1.0")
    assert not output.exists()


@pytest.mark.parametrize("relative", [
    "vendor/cnequity/src/cnequity/__init__.py",
    "packages/khquant_research/app/vendor/czsc/crates/czsc-utils/data/synthetic.csv",
    "packages/khquant_research/app/my_strategy/docs/history.md",
])
def test_changed_frozen_input_is_rejected_before_any_output_is_created(
        tmp_path, exporter, source_tree, relative):
    root, _ = source_tree
    (root / relative).write_bytes(b"changed after the original freeze")
    output = tmp_path / "must-not-exist"
    with pytest.raises(ValueError, match="Frozen input changed"):
        exporter.build_release(root, output, "0.1.0")
    assert not output.exists()


def test_selection_to_copy_mutation_cannot_create_an_archive(tmp_path, exporter, source_tree, monkeypatch):
    root, _ = source_tree
    select = exporter.select_files

    def mutate_after_selection(source):
        selected, provenance = select(source)
        (root / "vendor/cnequity/src/cnequity/__init__.py").write_bytes(b"mutation between selection and copy")
        return selected, provenance

    monkeypatch.setattr(exporter, "select_files", mutate_after_selection)
    output = tmp_path / "interrupted-export"
    with pytest.raises(ValueError, match="Source changed during export"):
        exporter.build_release(root, output, "0.1.0")
    assert not list(output.glob("*.zip"))
    assert not (output / "SHA256SUMS.txt").exists()


def test_identical_source_bytes_produce_identical_zip_sha_despite_mtime_changes(
        tmp_path, exporter, source_tree):
    root, _ = source_tree
    first = exporter.build_release(root, tmp_path / "first", "0.1.0")
    for path in root.rglob("*"):
        if path.is_file():
            os.utime(path, (1234567890, 1234567890))
    second = exporter.build_release(root, tmp_path / "second", "0.1.0")
    assert first["sha256"] == second["sha256"]
    assert Path(first["archive"]).read_bytes() == Path(second["archive"]).read_bytes()
    with zipfile.ZipFile(second["archive"]) as bundle:
        assert all(info.date_time == (2020, 1, 1, 0, 0, 0) for info in bundle.infolist())


def test_public_repository_rebuild_needs_no_original_private_manifests(tmp_path, exporter, source_tree):
    root, _ = source_tree
    first = exporter.build_release(root, tmp_path / "original", "0.1.0")
    repository = Path(first["repository"])
    assert not (repository / "docs/provenance/cnequity_source.json").exists()
    assert not (repository / "docs/provenance/khquant_source.json").exists()
    rebuilt_exporter = load_exporter(repository / "scripts/prepare_release.py")
    rebuilt = rebuilt_exporter.build_release(repository, tmp_path / "rebuilt", "0.1.0")
    assert rebuilt["sha256"] == first["sha256"]
    assert snapshot(Path(rebuilt["repository"])) == snapshot(repository)


def declare_patch(root, exporter):
    relative = "src/cnequity/__init__.py"
    source = root / "vendor/cnequity" / relative
    patch = dict(source_manifest="cnequity_source.json", path=relative,
                 original_sha256=exporter.sha256(source), reason="Synthetic reviewed integration patch")
    source.write_bytes(b"reviewed local source patch\n")
    patch["sha256"] = exporter.sha256(source)
    write(root, "docs/provenance/local_patches.json", json.dumps(dict(schema=1, patches=[patch])))
    return patch


def test_reviewed_patch_preserves_original_manifest_and_public_rebuild(tmp_path, exporter, source_tree):
    root, _ = source_tree
    original = (root / "docs/provenance/cnequity_source.json").read_bytes()
    patch = declare_patch(root, exporter)
    first = exporter.build_release(root, tmp_path / "patched", "0.1.0")
    assert (root / "docs/provenance/cnequity_source.json").read_bytes() == original
    repository = Path(first["repository"])
    provenance = json.loads((repository / "docs/provenance/sources.json").read_text())
    assert provenance["local_patches"] == [patch]
    assert json.loads((repository / "docs/provenance/local_patches.json").read_text())["patches"] == [patch]
    rebuilt = load_exporter(repository / "scripts/prepare_release.py").build_release(repository, tmp_path / "rebuilt-patched", "0.1.0")
    assert rebuilt["sha256"] == first["sha256"]


@pytest.mark.parametrize("field", ["original_sha256", "sha256", "path"])
def test_local_patch_must_match_original_and_reviewed_content(tmp_path, exporter, source_tree, field):
    root, _ = source_tree
    patch = declare_patch(root, exporter)
    patch[field] = "unknown.py" if field == "path" else "0" * 64
    write(root, "docs/provenance/local_patches.json", json.dumps(dict(schema=1, patches=[patch])))
    output = tmp_path / "rejected-patch"
    with pytest.raises(ValueError):
        exporter.build_release(root, output, "0.1.0")
    assert not output.exists()


def test_reviewed_public_docs_replace_host_history_without_copying_it(tmp_path, exporter, source_tree):
    root, _ = source_tree
    for name in ("PIPELINE.md", "SHARPE_OPTIMIZATION_PLAN.md"):
        write(root, f"docs/{name}", PRIVATE)
        write(root, f"docs/public/{name}", b"Reviewed public usage guide\n")
    result = exporter.build_release(root, tmp_path / "public-docs", "0.1.0")
    repository = Path(result["repository"])
    assert not (repository / "docs/public").exists()
    for name in ("PIPELINE.md", "SHARPE_OPTIMIZATION_PLAN.md"):
        assert (repository / "docs" / name).read_bytes() == b"Reviewed public usage guide\n"
