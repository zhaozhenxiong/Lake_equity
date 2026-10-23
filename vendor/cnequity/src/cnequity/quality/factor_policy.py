"""Factor semantics are advisory; malformed stored data remains an error."""

from __future__ import annotations

# Name checks explicitly. Ignoring an entire dataset would hide invalid numbers,
# corrupt Parquet, duplicate keys and publication failures.
_ADVISORY = frozenset(
    {
        "adj_factor_continuity",
        "adj_factor_pre_close_divergence",
        "adj_factor_action_implies_nonpositive_price",
        "adj_factor_corporate_action_divergence",
        "adj_factor_computed_vendor_divergence",
        "adj_factor_computed_unpriced_action",
        "adj_factor_arbitration",
        "adj_close_discontinuity",
        "missing_corporate_action",
        "missing_corporate_action_delisted",
        "unrecorded_ex_event",
        "unstepped_corporate_action",
        "repair_factor_action_contradictions",
        "stock_event_factor",
        "fund_share_factor",
    }
)


def factor_advisory(finding: dict) -> bool:
    check = str(finding.get("check", ""))
    return check.removesuffix("_overflow") in _ADVISORY


def advisory_factor_findings(findings: list[dict]) -> list[dict]:
    """Normalize current and retained findings at every audit boundary."""
    result = []
    for finding in findings:
        if not factor_advisory(finding):
            result.append(finding)
            continue
        item = {**finding, "gate_impact": "none"}
        if item.get("severity") == "error":
            item["severity"] = "warning"
        result.append(item)
    return result
