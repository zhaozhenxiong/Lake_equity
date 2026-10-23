from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys


@dataclass(frozen=True)
class Settings:
    root: Path
    port: int = 8130

    @classmethod
    def load(cls, root: str | Path | None = None) -> "Settings":
        return cls(Path(root or os.environ.get("LAKE_EQUITY_HOME") or Path(__file__).resolve().parents[2]).resolve(),
                   int(os.environ.get("LAKE_EQUITY_PORT", "8130")))

    @property
    def raw_db(self) -> Path:
        return Path(os.environ.get("LAKE_EQUITY_RAW_DB") or self.root / "var/research_inputs/current/khquant_raw.db").resolve()

    @property
    def data_root(self) -> Path:
        return self.root / "var/research"

    @property
    def metadata_root(self) -> Path:
        return self.data_root / "metadata"

    @property
    def artifact_root(self) -> Path:
        return self.data_root / "artifacts"

    @property
    def source_db(self) -> Path:
        return self.root / "var/legacy/raw/khquant_raw.db"

    @property
    def research_app(self) -> Path:
        return self.root / "packages/khquant_research/app"

    def configure(self) -> None:
        for path in (self.root / "src", self.research_app, self.root / "vendor/cnequity/src"):
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))
        values = {
            "KHQUANT_ENV_SCHEMA": "2",
            "KHQUANT_PROJECT_ROOT": str(self.research_app),
            "KHQUANT_DATA_ROOT": str(self.data_root),
            "KHQUANT_METADATA_ROOT": str(self.metadata_root),
            "KHQUANT_ARTIFACT_ROOT": str(self.artifact_root),
            "KHQUANT_RAW_DB": str(self.raw_db),
            "KHQUANT_KRONOS_ROOT": str(self.root / "var/models/kronos"),
            "KHQUANT_LOG_ROOT": str(self.root / "var/logs/research"),
            "KHQUANT_COMPUTE_DEVICE": os.environ.get("LAKE_EQUITY_DEVICE", "cpu"),
            "KHQUANT_CPU_WORKERS": "4",
            "TZ": "Asia/Shanghai",
        }
        os.environ.update(values)
        os.environ.setdefault("PROCESSOR_ARCHITECTURE", "AMD64")
        for path in (self.metadata_root, self.artifact_root / "runs", self.root / "var/logs", self.raw_db.parent):
            path.mkdir(parents=True, exist_ok=True)
