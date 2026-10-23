# Source health: where this table comes from

These sources are not exclusive to this project — AkShare, various data-fetching skills and your own scrapers hit the same set of endpoints. When one of them changes, there is usually nowhere to look it up, and you end up spending half a day suspecting your own code first. The CLI uses small-scale probes to record reachability from the current egress, with results tagged with a time and an egress label.

## How to use it

```bash
cne sources probe --list           # offline view of valid names
cne sources limits                 # offline check of shared cooldowns and today's budget
cne sources probe --only sina --vantage cn  # explicitly test one endpoint
cne sources probe --stale-only --vantage cn  # after the daily update, re-probe only ordinary endpoints; high-risk endpoints are skipped by default
cne serve                    # open the dashboard, then choose the "数据源" (Data sources) page
```

**Probing happens in the CLI; display happens in serve.** The health page only shows existing reports — it does not go and request a dozen third-party hosts on your behalf, for the same reason it does not trigger collection: an unauthenticated local service should not be something a stray browser tab can point at other people's APIs.

Test only a few sources; select only the source family you currently need to diagnose:

```bash
cne sources probe --only cninfo
```

`--vantage` is something you **must fill in carefully**: it records which egress this probe was sent from. See "[Why the vantage point determines the conclusion](#why-vantage-determines-the-conclusion)" below. Each vantage gets one file, and the page renders them side by side.

## Five statuses

| Status | Meaning |
|------|------|
| **Available** | Returned real data |
| **Empty response** | Connected, and HTTP was fine, but no data |
| **Refused** | Reached but refused (403 / risk-control page / challenge) |
| **Unreachable** | Could not connect, or timed out |
| **Not probed** | Turned off in the configuration, or excluded by `--only` |

### Why "empty response" is its own category

**HTTP 200 does not mean available.** EastMoney returns risk-control pages with 200, Sina returns empty arrays with 200, and THS likewise returns 200 with an empty response when rate-limiting. A probe that only looks at the status line would judge all three healthy.

So every probe asserts on the **response body**: clist must have `total`, kline must have `klines`, the SZSE export must start with `PK` (xlsx is a zip; a risk-control page is HTML), and the SW download must start with the OLE2 magic.

"Empty response" is singled out because it looks healthier than a failure but is actually more dangerous — backfills get silently truncated, and nothing shows from the outside.

### Why "refused" does not count as "down"

"It refused you" and "it isn't there" point to completely different fixes. For the former, first stop requests to the same source and check cooldown/authentication status; for the latter, you also need to check routing, timeouts and the source service. Do not retry from a different IP to work out the cause.

## Why the vantage point determines the conclusion {#why-vantage-determines-the-conclusion}

The same host can give different results over different network paths, egresses and times. One failure cannot be taken to mean an entire region is unavailable, and a 502 or an empty reply alone cannot prove the IP is banned. Keep the status and time first, then cool down and diagnose according to [source protection](fetch-policy.md).

So every report carries a `vantage` label, and the page puts different vantages **side by side** rather than merging them into one conclusion — merging would invent a "fact" that no single probe actually measured.

Run once in each of two networks and the page has two columns:

```bash
cne sources probe --stale-only --vantage cn          # mainland egress
cne sources probe --stale-only --vantage overseas    # overseas egress
```

The file name does not determine the column name; the `vantage` field in the JSON does. Identical names overwrite, so running again from the same egress just refreshes that column.

## One probe is not an SLA

Each source runs a bounded probe; some sources need a handshake, pagination or several file downloads. Exchange probes try at most two business days: SHFE, CZCE, GFEX and DCE only verify the futures daily file, and their separate options files must be validated by real collection; CFFEX's single file contains both futures and options. Baostock's login, query and logout each take a request as well. A passing probe only means this request and its response validation succeeded; it does not mean large-scale fetching will achieve the same success rate. It shares collection's rate limits, cooldowns and existing circuit breakers, and should not be polled at high frequency. A misspelled or blank `--only` name raises an error before any network request.

`dce`, `ths_pages` and `baostock` have known challenges or cumulative request costs; `cni` downloads complete history files and is also a high-cost probe. Full-table probes, both the default and `--stale-only`, mark them "not probed", but can reuse real-collection validation evidence from the same egress within the last 12 hours. Only after ruling out conflicts with same-source tasks and checking `sources limits` should you diagnose them one at a time explicitly with `--only dce` and the like; stop on a refusal, and do not keep probing to discover the cooldown length. Their active probes are not a mandatory sampling gate for the core SLO.
Probing is **serial**. These are exactly the hosts the daily pipeline depends on; firing a dozen requests at once would have the health check itself create the very failure it is supposed to observe.

## Probes use the adapters' own code

URL constants, EastMoney's authentication headers, the Chrome TLS impersonation SSE requires, THS rate limiting, TDX's binary protocol — they all use the same code the pipeline uses. When an adapter changes, the probe changes with it; a green probe with a red pipeline cannot happen because each side kept its own copy of the URL.

The converse also holds: the date the probe needs is **the most recent business day three days ago**, not today. Several endpoints have no current-day data before the close, and a table that turns red every morning would train people to stop looking at it.

## Where reports are stored

`{data_root}/meta/source_health/<vantage>.json`, alongside the lake's other metadata. `cne serve` does not read it at startup, only when the source health API is accessed — so just run the probe and refresh the page, no restart needed.

**There is no mandatory scheduled publishing locally.** If you want it to run daily, hook it into your existing scheduler (see the [runbook](runbook.md)):

```bash
cne sources probe --stale-only --vantage cn >> logs/source-health.log 2>&1
```

**A failed probe does not make the command fail.** A source turning red is this command's **output**, not its error; if your scheduler needs a gate, parse `status` in the JSON and decide by business need whether to block the daily update.

The repository also provides a GitHub Actions workflow that probes key sources on weekdays and ordinary endpoints weekly: from an overseas runner it
probes TDX, the first BSE page, Sina, EastMoney and CNINFO,
writes a text summary to the Job Summary and uploads a JSON artifact. This report represents only
the `github-actions` vantage; a mainland machine can run `cne sources probe --stale-only --vantage cn`, and an overseas `blocked`
should not be misread as a global outage.

The workflow restores and saves the Actions cache of `meta/source_health` with a unique run key, so the 30-day
SLO uses immutable samples across runs rather than a single probe inside a temporary runner. Each report is also kept as a 30-day
artifact; if the cache is lost, the SLO fails closed for lack of samples and does not interpret an empty history as healthy.

## Source SLO and resilience

`cne sources probe` writes both the latest report and an immutable per-vantage history sample by default; an explicit `--out` is only for a one-off
export. Once samples have accumulated, run:

```bash
cne sources slo --window-days 30 --minimum-observations 10 --enforce
cne sources resilience --enforce
```

Active probes and passive evidence from real collection are counted separately; when there are enough samples, active probes take precedence, otherwise passive evidence is used, and the two kinds of samples are never mixed into one success rate. The first command computes per probe/vantage, using only the last non-skipped probe of each UTC day; skipped items do not count as failures,
and a critical source that lacks the minimum cross-day samples, or whose samples are expired, fails. Same-day retries can verify recovery but cannot be used to inflate the SLO sample count.
Three consecutive failures write to `meta/source_health/incidents.json` with a stable `dedupe_key`; later CI reruns update
the same incident payload. The second command derives concentration and failure-domain blast radius from DatasetSpec, and verifies whether core datasets
have truly independent fallback sources. Adjustment factors belong to the research layer: the single-source risk of Sina is reported, but a failure there marks the run
`degraded`, and does not roll back committed raw daily bars revisions or misclassify them as a core failure.



## Related documentation

- [CLI](../reference/cli.md#cne-sources) · [Product scope](../architecture/overview.md) · [Per-source limits](../datasets/sources.md) · [Troubleshooting](troubleshooting.md)

## Availability targets are tiered by vantage

Availability is tracked separately per **(source, network egress)**. Below is the current client gate policy, not a data provider's SLA, and not a guarantee of availability in any region. It is based on the stored cross-day samples:

| vantage class | critical target | others |
|---|---|---|
| `cn` | 99% | 95% |
| `overseas` | 90% | 85% |
| undeclared | counted as `cn` | |

Classification looks only at the **prefix** of `CNE_SOURCE_VANTAGE`, not at place names: `cn`, `cn-sh` and `cn_aliyun` are mainland;
`overseas`, `overseas-eu` and `overseas_aws` are not. Place names are never the criterion.

**An undeclared vantage is treated with the strict tier** — the gate should not give a discount to an egress nobody declared, and declaring one takes just one environment variable.

The different tiers are an operations policy; missing samples, stale evidence or persistent failures of critical sources still need to be addressed, and an unavailable source cannot be marked healthy by changing its label. Personal measurements stay in local run records.
