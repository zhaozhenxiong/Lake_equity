from __future__ import annotations

import hashlib
import json
import re
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from cnequity.config import Config, load_config
from cnequity.domain.datasets import DATASETS
from cnequity.query.parquet_scan import scan_parquet_root
from cnequity.storage.atomic import write_json_atomic
from cnequity.storage.revisions import RevisionStore


class Unavailable(ValueError):
    pass


def _canonical(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _hex(value, length: int) -> bool:
    return isinstance(value, str) and re.fullmatch(f"[0-9a-f]{{{length}}}", value) is not None


def _inside(path: Path, root: Path) -> Path:
    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise ValueError("Market read path escapes its configured root") from exc
    if ".." in relative.parts:
        raise ValueError("Unsafe market read path")
    cursor = root
    if cursor.is_symlink():
        raise ValueError("Market read root cannot be a symlink")
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("Market read path cannot contain symlinks")
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("Market read path escapes its configured root")
    return resolved


class MarketStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.config_path = self.root / "configs" / "cnequity.toml"
        if self.config_path.is_file():
            configured = tomllib.loads(self.config_path.read_text(encoding="utf-8"))
            data_root = Path(configured.get("data", {}).get("root", "var/lake")).expanduser()
            if not data_root.is_absolute():
                data_root = self.root / data_root
            self.data_root = _inside(data_root, self.root / "var")
            self.config = load_config(self.config_path)
            self.config.data_root = self.data_root
        else:
            self.data_root = self.root / "var" / "lake"
            self.config = Config(data_root=self.data_root, tdx_allow_mock=False)
        self.reads_root = self.root / "var" / "market_views" / "reads"
        self.revisions = RevisionStore(self.config.meta_root, self.config.curated_root, self.config.derived_root, create=False)

    def _manifest(self, read_ref: str) -> dict:
        if not _hex(read_ref, 64):
            raise ValueError("Invalid market read_ref")
        path = _inside(self.reads_root / f"{read_ref}.json", self.root / "var")
        if not path.is_file():
            raise Unavailable("Market read_ref is not retained")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Invalid market read manifest")
        identity = {key: value for key, value in payload.items() if key != "read_ref"}
        if payload.get("read_ref") != read_ref or hashlib.sha256(_canonical(identity)).hexdigest() != read_ref:
            raise ValueError("Market read manifest identity mismatch")
        if payload.get("schema_version") != 1 or payload.get("data_root") != str(self.data_root):
            raise ValueError("Market read manifest belongs to another lake")
        if set(payload.get("datasets", {})) != set(DATASETS):
            raise ValueError("Market read manifest dataset registry mismatch")
        expected_map = {name: item["revision_id"] for name, item in payload["datasets"].items() if item.get("available")}
        if payload.get("revision_map") != expected_map:
            raise ValueError("Market read manifest revision map mismatch")
        return payload

    def _generation(self, dataset: str, item: dict) -> Path:
        if dataset not in DATASETS:
            raise ValueError(f"Unknown market dataset: {dataset}")
        if not item.get("available"):
            raise Unavailable(item.get("reason", "No published generation"))
        revision_id = item.get("revision_id")
        revision = item.get("revision")
        if not _hex(revision_id, 32) or isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("Market dataset has no positive published revision")
        expected_relative = Path("revisions") / "data" / dataset / revision_id
        root = _inside(self.config.meta_root / expected_relative, self.config.meta_root)
        if item.get("path") != str(root) or not root.is_dir():
            raise Unavailable(f"Pinned generation is missing or mismatched: {dataset}")
        receipt_relative = Path("revisions") / dataset / f"{revision:08d}-{revision_id}.json"
        if item.get("receipt_path") != receipt_relative.as_posix():
            raise ValueError("Pinned receipt path mismatch")
        receipt_path = _inside(self.config.meta_root / receipt_relative, self.config.meta_root)
        if not receipt_path.is_file():
            raise Unavailable(f"Pinned receipt is missing: {dataset}")
        receipt_bytes = receipt_path.read_bytes()
        if hashlib.sha256(receipt_bytes).hexdigest() != item.get("receipt_sha256"):
            raise ValueError("Pinned receipt bytes changed")
        receipt = json.loads(receipt_bytes)
        if receipt != item.get("receipt"):
            raise ValueError("Pinned receipt identity mismatch")
        if (receipt.get("dataset"), receipt.get("revision"), receipt.get("revision_id"), receipt.get("run_id"), receipt.get("generation_path")) != (dataset, revision, revision_id, item.get("run_id"), expected_relative.as_posix()):
            raise ValueError("Pinned receipt belongs to another generation")
        records = receipt.get("generation_files", [])
        if not records:
            raise Unavailable(f"Pinned generation has no file evidence: {dataset}")
        expected_files = set()
        for record in records:
            relative = Path(record.get("path", ""))
            if relative.is_absolute() or len(relative.parts) < 2 or relative.parts[0] != dataset or ".." in relative.parts or relative.suffix != ".parquet":
                raise ValueError("Unsafe pinned generation file")
            path = _inside(root.joinpath(*relative.parts[1:]), root)
            if not _hex(record.get("sha256"), 64) or not path.is_file() or path.stat().st_size != record.get("size_bytes"):
                raise Unavailable(f"Pinned generation file is missing or changed: {dataset}")
            expected_files.add(path)
        actual_files = {_inside(path, root) for path in root.rglob("*.parquet")}
        if actual_files != expected_files:
            raise ValueError("Pinned generation file set mismatch")
        return root

    def _scan_generation(self, dataset: str, item: dict) -> pl.LazyFrame:
        root = self._generation(dataset, item)
        return scan_parquet_root(root, dataset=dataset, partition_col=DATASETS[dataset].query_date_col, committed=False)

    def capture(self, read_ref: str | None = None) -> dict:
        if read_ref is not None:
            payload = self._manifest(read_ref)
            for name, item in payload["datasets"].items():
                if item.get("available"):
                    self._generation(name, item)
            return payload
        datasets = {}
        for name, spec in DATASETS.items():
            item = {"available": False, "rows": 0, "revision_id": None, "revision": None, "run_id": None, "path": None, "receipt": None, "units": spec.unit_contract, "date_column": spec.query_date_col, "coverage_start": None, "coverage_end": None, "layer": spec.layer, "pit": spec.pit}
            datasets[name] = item
            pointer = self.revisions.current_pointer(name)
            if pointer is None or pointer.get("revision", 0) < 1:
                item["reason"] = "No positive published revision; mutable or revision-zero data is not eligible"
                continue
            revision_id = pointer.get("revision_id")
            revision = pointer["revision"]
            if not _hex(revision_id, 32):
                raise ValueError(f"Invalid published revision id: {name}")
            relative = Path("revisions") / name / f"{revision:08d}-{revision_id}.json"
            receipt_path = _inside(self.config.meta_root / relative, self.config.meta_root)
            receipt_bytes = receipt_path.read_bytes()
            receipt = json.loads(receipt_bytes)
            item.update(available=True, revision_id=revision_id, revision=revision, run_id=receipt.get("run_id"), path=str(self.config.meta_root / "revisions" / "data" / name / revision_id), receipt=receipt, receipt_path=relative.as_posix(), receipt_sha256=hashlib.sha256(receipt_bytes).hexdigest())
            frame = self._scan_generation(name, item)
            expressions = [pl.len().alias("rows")]
            if spec.query_date_col and spec.query_date_col in frame.collect_schema():
                expressions.extend([pl.col(spec.query_date_col).min().cast(pl.String).alias("coverage_start"), pl.col(spec.query_date_col).max().cast(pl.String).alias("coverage_end")])
            item.update(frame.select(expressions).collect().row(0, named=True))
            item["publication_coverage"] = receipt.get("metadata", {}).get("coverage", {})
            item["coverage_completeness"] = "not_assessed"
        payload = {"schema_version": 1, "captured_at": datetime.now(timezone.utc).isoformat(), "data_root": str(self.data_root), "datasets": datasets, "revision_map": {name: item["revision_id"] for name, item in datasets.items() if item["available"]}, "atomic_across_datasets": False, "historical_completeness": "not_assessed", "semantics": "raw_published_observations", "notes": ["Independent published generations captured sequentially; not an atomic cross-table PIT snapshot.", "Generation file identities follow CNEquity immutable receipts; full file bytes are not rehashed on each view."]}
        payload["read_ref"] = hashlib.sha256(_canonical(payload)).hexdigest()
        path = _inside(self.reads_root / f"{payload['read_ref']}.json", self.root / "var")
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, payload, indent=2)
        return payload

    def read_snapshot(self, read_ref: str) -> dict:
        """Restore manifest identity only; scan validates the selected generation."""
        return self._manifest(read_ref)

    def scan(self, dataset: str, snapshot: dict) -> pl.LazyFrame:
        if dataset not in DATASETS:
            raise ValueError(f"Unknown market dataset: {dataset}")
        retained = self._manifest(snapshot.get("read_ref"))
        if retained != snapshot:
            raise ValueError("Market snapshot differs from its retained manifest")
        return self._scan_generation(dataset, retained["datasets"][dataset])

    def frame(self, dataset: str, snapshot: dict) -> pl.DataFrame:
        return self.scan(dataset, snapshot).collect()
