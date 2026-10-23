"""Text and JSON rendering for the doctor report."""

from __future__ import annotations

import click

from cnequity.diagnostics.report import Report, Severity
from cnequity.i18n import _, localize

_MARK: dict[Severity, tuple[str, str]] = {
    Severity.OK: ("OK  ", "green"),
    Severity.WARN: ("WARN", "yellow"),
    Severity.ERROR: ("FAIL", "red"),
}


def _style(text: str, color: str) -> str:
    return click.style(text, fg=color)


def render_text(report: Report) -> list[str]:
    lines: list[str] = [_("Environment")]
    width = max(len(k) for k in report.environment)
    for key, value in report.environment.items():
        lines.append(f"  {key.ljust(width)}  {value}")

    lines.append("")
    lines.append(_("Dependencies"))
    for status in report.packages:
        mark, color = _MARK[Severity.OK if status.importable else Severity.ERROR]
        note = "OK" if status.importable else _("cannot be imported")
        lines.append(f"  {_style(mark, color)}  {status.package.module} — {note}")

    if report.findings:
        lines.append("")
        lines.append(_("Findings"))
        for finding in report.findings:
            mark, color = _MARK[finding.severity]
            lines.append(f"  {_style(mark, color)}  {localize(finding.title)}")
            for detail_line in localize(finding.detail).splitlines():
                lines.append(f"        {detail_line}")
            if finding.fix:
                fix_lines = localize(finding.fix).splitlines()
                lines.append(f"        {_style('→', 'cyan')} {fix_lines[0]}")
                # Repair steps arrive as one line per command; keep them aligned
                # under the arrow so they stay copy-pasteable.
                lines.extend(f"          {extra}" for extra in fix_lines[1:])
            lines.append("")

    errors, warnings = len(report.errors), len(report.warnings)
    if errors:
        lines.append(
            _style(
                _("{errors} problem(s) need attention, {warnings} warning(s).").format(
                    errors=errors, warnings=warnings
                ),
                "red",
            )
        )
    elif warnings:
        lines.append(
            _style(
                _("No fatal problems, {warnings} warning(s).").format(warnings=warnings), "yellow"
            )
        )
    else:
        # Say what was checked, not just that it passed. `doctor` inspects the
        # environment, dependencies and config — never the lake — so a bare
        # "everything is fine" reads as a clean bill of health for a lake whose
        # daily job may have been failing for a fortnight.
        lines.append(_style(_("Environment, dependencies and config are fine."), "green"))
    lines.append(
        _(
            "doctor checks only the environment, dependencies and config; for the lake, "
            "use `cne status --datasets`."
        )
    )
    return lines


def to_dict(report: Report) -> dict:
    return {
        "environment": report.environment,
        "packages": [
            {
                "module": s.package.module,
                "importable": s.importable,
                "purpose": s.package.purpose,
            }
            for s in report.packages
        ],
        "findings": [
            {
                "severity": f.severity.value,
                "title": f.title,
                "detail": f.detail,
                "fix": f.fix,
            }
            for f in report.findings
        ],
        "ok": report.ok,
    }
