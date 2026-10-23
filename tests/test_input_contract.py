from __future__ import annotations

import hashlib
import json
import os
import sqlite3

import pytest

from lake_equity.data import InputContract


@pytest.fixture
def input_file(tmp_path):
    database = tmp_path / "var" / "research_inputs" / "current" / "khquant_raw.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE prices(value INTEGER)")
        connection.execute("INSERT INTO prices VALUES(1)")
    manifest = {"manifest_id": "synthetic-frozen-input", "sqlite_sha256": hashlib.sha256(database.read_bytes()).hexdigest(), "status": "complete", "rows": 1}
    database.with_suffix(".manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return database


def test_validate_binds_database_manifest_and_stat(input_file):
    binding = InputContract(input_file).validate()
    assert binding.manifest_id == "synthetic-frozen-input"
    assert binding.sqlite_sha256 == hashlib.sha256(input_file.read_bytes()).hexdigest()
    assert binding.manifest_sha256 == hashlib.sha256(binding.manifest_path.read_bytes()).hexdigest()
    assert len(binding.stat_identity) == 5


def test_validation_rehashes_after_database_changes(input_file, monkeypatch):
    from lake_equity.data import input_contract

    actual_sha = input_contract._sha256
    hashed = []

    def record(path):
        hashed.append(path)
        return actual_sha(path)

    monkeypatch.setattr(input_contract, "_sha256", record)
    contract = InputContract(input_file)
    contract.validate()
    contract.validate()
    assert len(hashed) == 2
    with sqlite3.connect(input_file) as connection:
        connection.execute("UPDATE prices SET value=2")
    with pytest.raises(ValueError, match="SHA256 differs"):
        contract.validate()
    assert len(hashed) == 3


def test_changed_manifest_invalidates_input_identity(input_file):
    contract = InputContract(input_file)
    contract.validate()
    path = input_file.with_suffix(".manifest.json")
    manifest = json.loads(path.read_text())
    manifest["sqlite_sha256"] = "0" * 64
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="SHA256 differs"):
        contract.validate()


@pytest.mark.parametrize("suffix", ["-wal", "-journal"])
def test_nonempty_sqlite_journal_is_rejected(input_file, suffix):
    input_file.with_name(input_file.name + suffix).write_bytes(b"active-transaction")
    with pytest.raises(ValueError, match="WAL or journal"):
        InputContract(input_file).validate()


def test_missing_manifest_fails_closed(input_file):
    input_file.with_suffix(".manifest.json").unlink()
    with pytest.raises(ValueError, match="manifest is missing"):
        InputContract(input_file).validate()


def test_mutation_during_hash_is_rejected(input_file, monkeypatch):
    from lake_equity.data import input_contract

    actual_sha = input_contract._sha256

    def mutate(path):
        result = actual_sha(path)
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE prices SET value=3")
        return result

    monkeypatch.setattr(input_contract, "_sha256", mutate)
    with pytest.raises(ValueError, match="changed during"):
        InputContract(input_file).validate()


def test_snapshot_is_an_independent_copy_with_retained_manifest(input_file):
    contract = InputContract(input_file)
    frozen = contract.pin_snapshot()
    assert frozen.raw_db == input_file.parents[1] / "snapshots" / "synthetic-frozen-input" / "khquant_raw.db"
    assert frozen.raw_db.stat().st_ino != input_file.stat().st_ino
    assert frozen.manifest_path.read_bytes() == input_file.with_suffix(".manifest.json").read_bytes()
    assert contract.pin_snapshot().sqlite_sha256 == frozen.sqlite_sha256
    with sqlite3.connect(input_file) as connection:
        connection.execute("UPDATE prices SET value=4")
    with sqlite3.connect(frozen.raw_db) as connection:
        assert connection.execute("SELECT value FROM prices").fetchone()[0] == 1


def test_corrupt_existing_snapshot_is_never_reused(input_file):
    contract = InputContract(input_file)
    frozen = contract.pin_snapshot()
    with sqlite3.connect(frozen.raw_db) as connection:
        connection.execute("UPDATE prices SET value=5")
    with pytest.raises(ValueError, match="SHA256 differs"):
        contract.pin_snapshot()


def test_changed_source_during_copy_is_not_published(input_file, monkeypatch):
    from lake_equity.data import input_contract

    actual_copy = input_contract.shutil.copyfile

    def mutate(source, destination):
        result = actual_copy(source, destination)
        with sqlite3.connect(source) as connection:
            connection.execute("UPDATE prices SET value=6")
        return result

    monkeypatch.setattr(input_contract.shutil, "copyfile", mutate)
    with pytest.raises(ValueError):
        InputContract(input_file).pin_snapshot()
    assert not (input_file.parents[1] / "snapshots" / "synthetic-frozen-input").exists()
    assert not list((input_file.parents[1] / "snapshots").glob("*.building"))


def test_manifest_identity_cannot_escape_snapshot_root(input_file):
    path = input_file.with_suffix(".manifest.json")
    manifest = json.loads(path.read_text())
    manifest["manifest_id"] = "../outside"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="unsafe"):
        InputContract(input_file).pin_snapshot()


def test_existing_hardlink_snapshot_is_rejected(input_file):
    destination = input_file.parents[1] / "snapshots" / "synthetic-frozen-input" / "khquant_raw.db"
    destination.parent.mkdir(parents=True)
    os.link(input_file, destination)
    destination.with_suffix(".manifest.json").write_bytes(input_file.with_suffix(".manifest.json").read_bytes())
    with pytest.raises(ValueError, match="hardlinks"):
        InputContract(input_file).pin_snapshot()


def test_same_stat_identity_does_not_hide_changed_sqlite_contents(input_file, monkeypatch):
    from lake_equity.data import input_contract

    identity = input_contract._identity(input_file)
    monkeypatch.setattr(input_contract, "_identity", lambda path: identity)
    contract = InputContract(input_file)
    contract.validate()
    with sqlite3.connect(input_file) as connection:
        connection.execute("UPDATE prices SET value=7")
    with pytest.raises(ValueError, match="SHA256 differs"):
        contract.validate()


def test_sqlite_commit_during_hash_is_detected_even_with_same_stat(input_file, monkeypatch):
    from lake_equity.data import input_contract

    identity = input_contract._identity(input_file)
    actual_sha = input_contract._sha256
    monkeypatch.setattr(input_contract, "_identity", lambda path: identity)

    def commit_after_hash(path):
        result = actual_sha(path)
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE prices SET value=8")
        return result

    monkeypatch.setattr(input_contract, "_sha256", commit_after_hash)
    with pytest.raises(ValueError, match="changed during"):
        InputContract(input_file).validate()
