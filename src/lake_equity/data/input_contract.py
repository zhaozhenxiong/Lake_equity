from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


def _identity(path: Path) -> tuple[int, int, int, int, int]:
    value = path.stat()
    if not stat.S_ISREG(value.st_mode):
        raise ValueError("Research input must be a regular SQLite file")
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _no_redirect(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)():
            raise ValueError("Research input and snapshot paths cannot use symbolic links or junctions")


def _no_journal(path: Path) -> None:
    for suffix in ("-wal", "-journal"):
        companion = Path(str(path) + suffix)
        if companion.is_file() and companion.stat().st_size:
            raise ValueError("Research input has an active WAL or journal; materialize a closed SQLite input first")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sqlite_header(path: Path) -> bytes:
    with path.open("rb") as handle:
        return handle.read(100)


@dataclass(frozen=True)
class InputBinding:
    manifest_id: str
    sqlite_sha256: str
    raw_db: Path
    manifest_path: Path
    manifest: dict
    stat_identity: tuple[int, int, int, int, int]
    manifest_sha256: str


class InputContract:
    def __init__(self, raw_db: str | Path, snapshots_root: str | Path | None = None):
        self.raw_db = Path(raw_db).expanduser().absolute()
        self.manifest_path = self.raw_db.with_suffix(".manifest.json")
        parent = next((candidate for candidate in self.raw_db.parents if candidate.name == "research_inputs"), self.raw_db.parent)
        self.snapshots_root = Path(snapshots_root).expanduser().absolute() if snapshots_root else parent / "snapshots"
        self._lock = threading.RLock()

    def validate(self) -> InputBinding:
        with self._lock:
            _no_redirect(self.raw_db)
            _no_redirect(self.manifest_path)
            if not self.raw_db.is_file():
                raise ValueError("Research input SQLite is missing; materialize the lake first")
            if not self.manifest_path.is_file():
                raise ValueError("Research input manifest is missing; an unbound SQLite input is not permitted")
            _no_journal(self.raw_db)
            before = _identity(self.raw_db)
            header_before = _sqlite_header(self.raw_db)
            encoded = self.manifest_path.read_bytes()
            manifest_sha = hashlib.sha256(encoded).hexdigest()
            try:
                manifest = json.loads(encoded)
            except (UnicodeDecodeError, ValueError) as error:
                raise ValueError("Research input manifest is not valid JSON") from error
            if not isinstance(manifest, dict):
                raise ValueError("Research input manifest must be an object")
            manifest_id = manifest.get("manifest_id")
            if not isinstance(manifest_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", manifest_id):
                raise ValueError("Research input manifest_id is missing or unsafe")
            expected = manifest.get("sqlite_sha256")
            if not isinstance(expected, str) or not re.fullmatch("[0-9a-f]{64}", expected):
                raise ValueError("Research input manifest requires a complete sqlite_sha256")
            if manifest.get("status") != "complete":
                raise ValueError("Research input manifest does not describe a completed materialization")
            actual = _sha256(self.raw_db)
            _no_journal(self.raw_db)
            after = _identity(self.raw_db)
            if before != after or header_before != _sqlite_header(self.raw_db) or hashlib.sha256(self.manifest_path.read_bytes()).hexdigest() != manifest_sha:
                raise ValueError("Research input changed during identity validation")
            if actual != expected:
                raise ValueError("Research SQLite SHA256 differs from its input manifest; materialize again before computing")
            return InputBinding(manifest_id, actual, self.raw_db, self.manifest_path, manifest, after, manifest_sha)

    def pin_snapshot(self) -> InputBinding:
        with self._lock:
            binding = self.validate()
            _no_redirect(self.snapshots_root)
            self.snapshots_root.mkdir(parents=True, exist_ok=True)
            destination = self.snapshots_root / binding.manifest_id
            _no_redirect(destination)
            frozen_db = destination / "khquant_raw.db"
            if destination.exists():
                return self._validate_snapshot(frozen_db, binding)
            temporary = self.snapshots_root / f".{binding.manifest_id}.{uuid4().hex}.building"
            temporary.mkdir()
            copied = temporary / "khquant_raw.db"
            copied_manifest = copied.with_suffix(".manifest.json")
            try:
                shutil.copyfile(self.raw_db, copied)
                with copied.open("rb+") as handle:
                    os.fsync(handle.fileno())
                current = self.validate()
                if current.stat_identity != binding.stat_identity or current.manifest_sha256 != binding.manifest_sha256:
                    raise ValueError("Research input changed while its snapshot was copied")
                encoded = binding.manifest_path.read_bytes()
                if hashlib.sha256(encoded).hexdigest() != binding.manifest_sha256:
                    raise ValueError("Research input manifest changed while its snapshot was copied")
                with copied_manifest.open("wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._validate_snapshot(copied, binding)
                try:
                    os.replace(temporary, destination)
                except OSError:
                    if not destination.is_dir():
                        raise
                return self._validate_snapshot(frozen_db, binding)
            finally:
                if copied_manifest.exists():
                    copied_manifest.unlink()
                if copied.exists():
                    copied.unlink()
                if temporary.exists():
                    temporary.rmdir()

    @staticmethod
    def _validate_snapshot(path: Path, original: InputBinding) -> InputBinding:
        if path.is_file() and path.stat().st_nlink != 1:
            raise ValueError("Frozen research snapshots cannot share writable hardlinks")
        frozen = InputContract(path).validate()
        if frozen.manifest_id != original.manifest_id or frozen.sqlite_sha256 != original.sqlite_sha256 or frozen.manifest_sha256 != original.manifest_sha256:
            raise ValueError("An existing frozen snapshot disagrees with the input identity")
        return frozen
