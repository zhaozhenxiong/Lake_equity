"""Public source verification uses only synthetic release files."""
import hashlib
import json
import sys

import pytest

from lake_equity import cli
from lake_equity.settings import Settings


def manifest(root, entries=None):
    source = root / "src/example.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        source.write_bytes(b"synthetic source\n")
    entry = dict(path="src/example.py", size=source.stat().st_size,
                 sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    body = dict(schema=1, kind="lake-equity-public-source", files=[entry] if entries is None else entries)
    (root / "release-manifest.json").write_text(json.dumps(body), encoding="utf-8")
    return body


def verify(root):
    return cli.verify_sources(Settings(root))


def test_public_manifest_verifies_without_private_provenance(tmp_path):
    manifest(tmp_path)
    result = verify(tmp_path)
    assert result == {"status": "ok", "sources": [dict(
        source="release-manifest.json", files_checked=1, failures=[])]}
    assert not (tmp_path / "docs").exists()
    private = tmp_path / "docs/provenance/cnequity_source.json"
    private.parent.mkdir(parents=True)
    private.write_text("invalid private manifest")
    assert verify(tmp_path) == result  # public release identity has precedence


@pytest.mark.parametrize("change,reason", [
    ("same_size", "sha256_mismatch"), ("size", "size_mismatch"), ("missing", "missing")])
def test_tampered_and_missing_source_fail(tmp_path, change, reason):
    manifest(tmp_path)
    source = tmp_path / "src/example.py"
    if change == "same_size":
        source.write_bytes(source.read_bytes().upper())
    elif change == "size":
        source.write_bytes(b"different length")
    else:
        source.unlink()
    result = verify(tmp_path)
    assert result["status"] == "failed"
    assert result["sources"][0]["failures"][0]["reason"] == reason


@pytest.mark.parametrize("path", [
    "", ".", "../outside", "src/../outside", "/outside", "C:/outside", "C:outside",
    "//server/share/file", "src\\example.py", "./src/example.py", "src//example.py",
    "release-manifest.json"])
def test_noncanonical_or_escaping_paths_are_rejected(tmp_path, path):
    entry = manifest(tmp_path)["files"][0]
    manifest(tmp_path, [{**entry, "path": path}])
    result = verify(tmp_path)
    assert result["status"] == "failed"
    assert result["sources"][0]["files_checked"] == 0
    assert result["sources"][0]["failures"][0]["reason"] == "invalid_file"


def test_duplicate_path_is_rejected(tmp_path):
    entry = manifest(tmp_path)["files"][0]
    manifest(tmp_path, [entry, entry])
    result = verify(tmp_path)
    assert result["status"] == "failed"
    assert "Duplicate" in result["sources"][0]["failures"][0]["error"]


@pytest.mark.parametrize("patch", [
    {"schema": True}, {"schema": 2}, {"kind": "private-source"}, {"files": None}, {"files": []}])
def test_invalid_schema_fails_without_legacy_fallback(tmp_path, patch):
    body = manifest(tmp_path)
    (tmp_path / "release-manifest.json").write_text(json.dumps({**body, **patch}))
    result = verify(tmp_path)
    assert result["status"] == "failed"
    assert [source["source"] for source in result["sources"]] == ["release-manifest.json"]


@pytest.mark.parametrize("patch", [{"size": True}, {"size": -1}, {"sha256": "not-a-hash"}, {"path": None}])
def test_invalid_file_metadata_fails(tmp_path, patch):
    entry = manifest(tmp_path)["files"][0]
    manifest(tmp_path, [{**entry, **patch}])
    assert verify(tmp_path)["status"] == "failed"


def test_corrupt_manifest_and_missing_legacy_manifests_return_failed(tmp_path):
    assert verify(tmp_path)["status"] == "failed"
    manifest(tmp_path)
    (tmp_path / "release-manifest.json").write_bytes(b"{broken json")
    result = verify(tmp_path)
    assert result["status"] == "failed"
    assert result["sources"][0]["failures"][0]["reason"] == "invalid_manifest"


@pytest.mark.parametrize("directory_link", [False, True])
def test_symlinks_are_rejected_even_when_the_target_is_inside_root(tmp_path, directory_link):
    entry = manifest(tmp_path)["files"][0]
    target = tmp_path / "src" if directory_link else tmp_path / entry["path"]
    link = tmp_path / "linked"
    try:
        link.symlink_to(target, target_is_directory=directory_link)
    except OSError as error:
        pytest.skip(f"Symlink creation unavailable: {error}")
    manifest(tmp_path, [{**entry, "path": "linked/example.py" if directory_link else "linked"}])
    result = verify(tmp_path)
    assert result["status"] == "failed"
    assert "symlink" in result["sources"][0]["failures"][0]["error"]


def test_legacy_source_manifests_remain_supported(tmp_path):
    provenance = tmp_path / "docs/provenance"
    provenance.mkdir(parents=True)
    for filename, base, relative in (
        ("cnequity_source.json", tmp_path / "vendor/cnequity", "source.py"),
        ("khquant_source.json", tmp_path / "packages/khquant_research", "LICENSE"),
    ):
        base.mkdir(parents=True)
        (base / relative).write_bytes(b"synthetic legacy source")
        payload = dict(files=[dict(path=relative, sha256=hashlib.sha256((base / relative).read_bytes()).hexdigest())])
        (provenance / filename).write_text(json.dumps(payload))
    result = verify(tmp_path)
    assert result["status"] == "ok"
    assert len(result["sources"]) == 2


@pytest.mark.parametrize("tamper", [None, "undeclared", "sha256", "original_sha256", "path"])
def test_legacy_patch_requires_exact_original_and_reviewed_hashes(tmp_path, tamper):
    test_legacy_source_manifests_remain_supported(tmp_path)
    source = tmp_path / "vendor/cnequity/source.py"
    patch = dict(source_manifest="cnequity_source.json", path="source.py",
                 original_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                 reason="Synthetic reviewed integration patch")
    source.write_bytes(b"reviewed integration patch")
    patch["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    if tamper in {"sha256", "original_sha256"}:
        patch[tamper] = "0" * 64
    elif tamper == "path":
        patch["path"] = "unknown.py"
    if tamper != "undeclared":
        (tmp_path / "docs/provenance/local_patches.json").write_text(json.dumps(dict(schema=1, patches=[patch])))
    result = verify(tmp_path)
    assert result["status"] == ("ok" if tamper is None else "failed")


@pytest.mark.parametrize("change", ["bad_schema", "bad_hash", "empty_reason", "duplicate"])
def test_invalid_local_patch_declaration_fails_closed(tmp_path, change):
    test_legacy_source_manifests_remain_supported(tmp_path)
    patch = dict(source_manifest="cnequity_source.json", path="source.py", original_sha256="a" * 64,
                 sha256="b" * 64, reason="Synthetic reviewed patch")
    payload = dict(schema=1, patches=[patch])
    if change == "bad_schema":
        payload["schema"] = True
    elif change == "bad_hash":
        patch["sha256"] = "not-a-hash"
    elif change == "empty_reason":
        patch["reason"] = ""
    else:
        payload["patches"].append(patch)
    (tmp_path / "docs/provenance/local_patches.json").write_text(json.dumps(payload))
    result = verify(tmp_path)
    assert result["status"] == "failed"
    assert result["sources"][0]["source"] == "local_patches.json"


@pytest.mark.parametrize("tamper", [False, True])
def test_cli_source_failure_has_nonzero_exit(tmp_path, monkeypatch, capsys, tamper):
    manifest(tmp_path)
    if tamper:
        (tmp_path / "src/example.py").write_bytes(b"modified")
    monkeypatch.setattr(sys, "argv", ["lake-equity", "--root", str(tmp_path), "verify-sources"])
    monkeypatch.setattr(Settings, "configure", lambda self: None)
    if tamper:
        with pytest.raises(SystemExit) as error:
            cli.main()
        assert error.value.code == 1
    else:
        cli.main()
    assert json.loads(capsys.readouterr().out)["status"] == ("failed" if tamper else "ok")
