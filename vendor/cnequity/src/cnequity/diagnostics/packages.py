"""Runtime package probes.

Every source package is a hard dependency, so a missing one means a damaged
environment rather than a forgotten install flag. The probe still earns its keep
because the failure is silent: the adapters below import their package lazily
inside a function, so a half-uninstalled environment surfaces as thin data on the
next scheduled run, not as an ImportError at startup.

Only lazily-imported packages are listed. `polars`, `duckdb` and friends are
imported at module scope — if one of those is missing, nothing runs at all and
there is no silent failure to catch.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.util import find_spec

from cnequity.i18n import N_


@dataclass(frozen=True)
class RequiredPackage:
    module: str
    purpose: str


REQUIRED_PACKAGES: tuple[RequiredPackage, ...] = (
    RequiredPackage("baostock", N_("historical backfill of valuation / ST / delisted bars")),
    RequiredPackage("snownlp", N_("on-demand stock_news sentiment ([sentiment] use_snownlp)")),
    RequiredPackage("pandas", N_("XLS·XLSX parsing of SW / CNI constituent history")),
    RequiredPackage("openpyxl", N_("XLSX parsing")),
    RequiredPackage("xlrd", N_("XLS parsing")),
)


@dataclass(frozen=True)
class PackageStatus:
    package: RequiredPackage
    importable: bool


def _importable(module: str) -> bool:
    """True when ``module`` can be located without executing it.

    ``find_spec`` imports parent packages only, which keeps the probe cheap —
    importing pandas or baostock for real would cost seconds per run.
    """
    try:
        return find_spec(module) is not None
    except (ImportError, ValueError):
        # A half-installed distribution can leave dist-info with no importable
        # package; treat that as missing rather than crashing the report.
        return False


def probe_packages() -> list[PackageStatus]:
    return [PackageStatus(p, _importable(p.module)) for p in REQUIRED_PACKAGES]
