"""Environment and dependency diagnostics behind `cne doctor`."""

from cnequity.diagnostics.metrics import (
    OFFLINE_BENCHMARK_SOURCES,
    persist_offline_benchmark,
    run_offline_benchmark,
)
from cnequity.diagnostics.packages import (
    REQUIRED_PACKAGES,
    PackageStatus,
    RequiredPackage,
    probe_packages,
)
from cnequity.diagnostics.report import (
    Finding,
    Report,
    Severity,
    build_report,
)

__all__ = [
    "REQUIRED_PACKAGES",
    "Finding",
    "PackageStatus",
    "Report",
    "RequiredPackage",
    "Severity",
    "build_report",
    "OFFLINE_BENCHMARK_SOURCES",
    "run_offline_benchmark",
    "persist_offline_benchmark",
    "probe_packages",
]
