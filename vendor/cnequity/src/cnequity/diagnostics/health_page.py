"""Render source-health reports into one self-contained page.

No CDN, no build step, no external font — same rule as the `cne serve` bundle,
and here the reason is sharper: this page is read by someone whose network is,
by construction, the thing in question. A stylesheet fetched from elsewhere is
one more host that can be why the page looks broken.

The page's job is to be *hard to misread*. Three things are therefore structural
rather than footnotes:

* **Vantages sit side by side and are never merged.** Several of these sources
  refuse non-mainland egress, so one column can be green and the other red for
  the same host at the same second, and both are true. Collapsing that into a
  single verdict would invent a fact neither probe established.
* **`blocked` is its own colour, not a shade of down.** "Refused you" and "is
  not there" send a reader to completely different fixes.
* **`empty` is called out as the dangerous one.** A source answering 200 with no
  rows is what silently truncates a backfill; it looks healthier than a failure
  and is worse.
"""

from __future__ import annotations

import html
from collections import Counter
from datetime import datetime, timezone

from cnequity.diagnostics.source_health import (
    PROBES,
    STATUS_LABELS,
    STATUS_MEANING,
    HealthReport,
    ProbeStatus,
)
from cnequity.i18n import N_, _, current_language, localize

# Vantage keys are free-form (the CLI takes whatever you pass), so this is a
# display hint rather than a whitelist: an unknown key still renders, under its
# own name.
VANTAGE_LABELS: dict[str, str] = {
    "cn": N_("mainland egress"),
    "overseas": N_("overseas egress"),
    "local": N_("this machine"),
}

DESCRIPTION = N_(
    "Daily availability probes of public A-share data sources: EastMoney, TDX, Sina, "
    "CNINFO, THS, baostock and the exchanges' official APIs, from mainland and "
    "overseas egress."
)

_STATUS_ORDER = (
    ProbeStatus.OK,
    ProbeStatus.EMPTY,
    ProbeStatus.BLOCKED,
    ProbeStatus.DOWN,
    ProbeStatus.SKIPPED,
)

_CSS = """
:root {
  color-scheme: light dark;
  --bg: #ffffff; --fg: #1f2328; --muted: #656d76; --line: #d0d7de;
  --card: #f6f8fa;
  --ok: #1a7f37; --ok-bg: #dafbe1;
  --empty: #9a6700; --empty-bg: #fff8c5;
  --blocked: #bc4c00; --blocked-bg: #fff1e5;
  --down: #cf222e; --down-bg: #ffebe9;
  --skipped: #656d76; --skipped-bg: #eaeef2;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117; --fg: #e6edf3; --muted: #8d96a0; --line: #30363d;
    --card: #161b22;
    --ok: #3fb950; --ok-bg: #12261e;
    --empty: #d29922; --empty-bg: #272115;
    --blocked: #db6d28; --blocked-bg: #2b1d13;
    --down: #f85149; --down-bg: #2d1517;
    --skipped: #8d96a0; --skipped-bg: #21262d;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2rem 1.25rem 4rem; background: var(--bg); color: var(--fg);
  font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
        "Hiragino Sans GB", "Microsoft YaHei", Helvetica, Arial, sans-serif;
}
main { max-width: 1080px; margin: 0 auto; }
h1 { font-size: 1.6rem; margin: 0 0 .3rem; }
h2 { font-size: 1.05rem; margin: 2.2rem 0 .6rem; }
.sub { color: var(--muted); margin: 0 0 1.4rem; }
a { color: inherit; }
.vantages { display: flex; flex-wrap: wrap; gap: .6rem; margin: 0 0 1.4rem; }
.vantage {
  border: 1px solid var(--line); border-radius: 6px; padding: .5rem .8rem;
  background: var(--card); font-size: .85rem;
}
.vantage b { display: block; font-size: .95rem; }
.scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
table { border-collapse: collapse; width: 100%; font-size: .9rem; }
th, td { text-align: left; padding: .55rem .6rem; border-bottom: 1px solid var(--line); }
th { font-weight: 600; color: var(--muted); font-size: .8rem; white-space: nowrap; }
tbody tr:hover { background: var(--card); }
.group td {
  background: var(--card); font-weight: 600; font-size: .8rem; color: var(--muted);
  border-bottom: 1px solid var(--line);
}
/* Membership of a shared-WAF group, marked on the rows themselves: a heading
   alone is positional, and the rows after the group would read as part of it. */
.grouped td:first-child { border-left: 3px solid var(--line); padding-left: .5rem; }
.src { min-width: 15rem; }
.src small { display: block; color: var(--muted); font-size: .78rem; font-weight: 400; }
.chip {
  display: inline-block; padding: .1rem .45rem; border-radius: 999px;
  font-size: .78rem; font-weight: 600; white-space: nowrap;
}
.lat { color: var(--muted); font-size: .78rem; margin-left: .35rem; }
.detail { color: var(--muted); font-size: .78rem; display: block; margin-top: .15rem;
          max-width: 22rem; word-break: break-word; }
.ok { color: var(--ok); background: var(--ok-bg); }
.empty { color: var(--empty); background: var(--empty-bg); }
.blocked { color: var(--blocked); background: var(--blocked-bg); }
.down { color: var(--down); background: var(--down-bg); }
.skipped { color: var(--skipped); background: var(--skipped-bg); }
.powers { color: var(--muted); font-size: .78rem; max-width: 20rem; }
.legend { display: grid; gap: .5rem; padding: 0; margin: 0 0 1rem; list-style: none; }
.legend li { display: flex; gap: .6rem; align-items: baseline; font-size: .86rem; }
.legend .chip { flex: none; }
.caveats { border-left: 3px solid var(--line); padding-left: .9rem; color: var(--muted);
           font-size: .88rem; }
.caveats p { margin: .5rem 0; }
footer { margin-top: 2.5rem; color: var(--muted); font-size: .82rem; }
"""


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _status(value: str) -> ProbeStatus:
    try:
        return ProbeStatus(value)
    except ValueError:
        return ProbeStatus.DOWN


def _vantage_label(key: str) -> str:
    return _(VANTAGE_LABELS[key]) if key in VANTAGE_LABELS else key


def _chip(status: ProbeStatus) -> str:
    return f'<span class="chip {status.value}">{_e(_(STATUS_LABELS[status]))}</span>'


def _summary(report: HealthReport) -> str:
    counts: dict[ProbeStatus, int] = {}
    for result in report.results:
        status = _status(result.status)
        counts[status] = counts.get(status, 0) + 1
    parts = [f"{_e(_(STATUS_LABELS[s]))} {counts[s]}" for s in _STATUS_ORDER if counts.get(s)]
    return " · ".join(parts)


def render_page(reports: list[HealthReport]) -> str:
    """One page covering every vantage in *reports*, ordered by the probe registry.

    Rows come from ``PROBES`` rather than from the reports, so a source that a
    vantage failed to probe at all shows as an explicit blank instead of
    silently vanishing from that column.
    """
    if not reports:
        raise ValueError("no reports to render")

    by_vantage = {r.vantage: {res.key: res for res in r.results} for r in reports}
    vantages = [r.vantage for r in reports]
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    head = [
        "<main>",
        "<h1>" + _e(_("A-share public data source health")) + "</h1>",
        '<p class="sub">'
        + _(
            "These are the dozen or so endpoints that AkShare, data-fetching skills and your own "
            "scrapers all depend on. This page reads the report <code>cne sources probe</code> "
            "last wrote into the lake — not a live probe; refreshing the page contacts no one."
        )
        + "</p>",
        '<div class="vantages">',
    ]
    for report in reports:
        head.append(
            f'<div class="vantage"><b>{_e(_vantage_label(report.vantage))}</b>{_summary(report)}<br><span>{_e(report.generated_at)}</span></div>'
        )
    head.append("</div>")

    rows = [
        '<div class="scroll"><table><thead><tr>',
        '<th class="src">' + _e(_("Data source")) + "</th>",
    ]
    rows += [f"<th>{_e(_vantage_label(v))}</th>" for v in vantages]
    rows.append("<th>" + _e(_("Affected datasets")) + "</th></tr></thead><tbody>")

    shared = Counter(probe.blast_radius for probe in PROBES)
    last_radius = None
    for probe in PROBES:
        if probe.blast_radius != last_radius:
            last_radius = probe.blast_radius
            if shared[probe.blast_radius] > 1:
                rows.append(
                    f'<tr class="group"><td colspan="{len(vantages) + 2}">'
                    + _e(
                        _(
                            "Shared risk-control surface: {radius} "
                            "(the {count} endpoints below go down together)"
                        ).format(radius=probe.blast_radius, count=shared[probe.blast_radius])
                    )
                    + "</td></tr>"
                )
        # The header alone is positional, and a reader takes "these 3 fail
        # together" to cover every row under it until the next heading. The
        # member rows carry the marker themselves so the group has a visible end.
        rows.append(f'<tr class="{"grouped" if shared[probe.blast_radius] > 1 else ""}">')
        note = f"<small>{_e(_(probe.note))}</small>" if probe.note else ""
        rows.append(
            f'<td class="src"><b>{_e(_(probe.label))}</b>'
            f"<small><code>{_e(probe.host)}</code></small>{note}</td>"
        )
        for vantage in vantages:
            result = by_vantage.get(vantage, {}).get(probe.key)
            if result is None:
                rows.append(f'<td><span class="chip skipped">{_e(_("not probed"))}</span></td>')
                continue
            status = _status(result.status)
            latency = (
                f'<span class="lat">{result.latency_ms} ms</span>'
                if result.latency_ms is not None
                else ""
            )
            detail = (
                f'<span class="detail">{_e(localize(result.detail))}</span>'
                if result.detail
                else ""
            )
            rows.append(f"<td>{_chip(status)}{latency}{detail}</td>")
        rows.append(f'<td class="powers">{_e(_(", ").join(probe.powers))}</td>')
        rows.append("</tr>")
    rows.append("</tbody></table></div>")

    legend = ["<h2>" + _e(_("What the states mean")) + "</h2>", '<ul class="legend">']
    for status in _STATUS_ORDER:
        legend.append(f"<li>{_chip(status)}<span>{_e(_(STATUS_MEANING[status]))}</span></li>")
    legend.append("</ul>")

    caveats = [
        "<h2>" + _e(_("How to read this table")) + "</h2>",
        '<div class="caveats">',
        _(
            "<p><b>One probe is not an SLA.</b> Each source gets one request per probe. Green "
            "means that one request succeeded, not that the next thousand will — for the few "
            "rate-limited sources here, that is a different question entirely.</p>"
        ),
        _(
            '<p><b>"Refused" is not "down".</b> Several sources refuse non-mainland egress at '
            "the WAF. For the same host in the same second, the mainland column can be green "
            "and the overseas column red, and both are true. So the two vantages sit side by "
            "side instead of being merged into one verdict.</p>"
        ),
        _(
            '<p><b>HTTP 200 is not "available".</b> EastMoney answers with a risk-control '
            "page under 200, Sina with an empty array under 200, and THS with 200 and an "
            "empty body when rate-limiting. So every probe asserts on the <b>response "
            'body</b>, not the status line; "empty" is its own state because it looks '
            "healthier than a failure and is actually more dangerous — a backfill silently "
            "truncates.</p>"
        ),
        _(
            "<p><b>Probes run the adapters' own code.</b> URL constants, EastMoney's auth "
            "headers, THS's pacing, the TDX binary protocol — all the same ones the daily "
            "pipeline uses; when an adapter changes, its probe changes with it.</p>"
        ),
        "</div>",
    ]

    footer = [
        "<footer>",
        _(
            "Page generated at {generated} · data written to <code>meta/source_health/</code> "
            "by <code>cne sources probe</code> · probe again: <code>cne sources probe "
            "--vantage cn</code>"
        ).format(generated=_e(generated)),
        "</footer>",
        "</main>",
    ]

    body = "\n".join(head + rows + legend + caveats + footer)
    return (
        "<!doctype html>\n"
        f'<html lang="{"en" if current_language() == "en" else "zh-CN"}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>" + _e(_("A-share public data source health")) + " · cnequity</title>"
        f'<meta name="description" content="{_e(_(DESCRIPTION))}">'
        f"<style>{_CSS}</style></head><body>\n{body}\n</body></html>\n"
    )
