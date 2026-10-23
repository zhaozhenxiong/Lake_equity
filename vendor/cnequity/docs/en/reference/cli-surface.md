# Network and write inventory

Checked against the Click command registry by `scripts/dev/sync_docs.py`. “When run” means when the command body executes; `--help` executes nothing. For source selection and limits, see [Fetching and source protection](../operations/fetch-policy.md).

| Command | Third-party fetching | Local effects |
|---|---|---|
| `cne audit` | conditional | Reads the lake and prints the audit; accesses sources when external comparison is enabled |
| `cne backfill` | when run | Backfills history, writing staging and published data; --plan only reads config and state |
| `cne check` | none | Reads status, freshness, audit results and stats; recomputes stale stats, --full reruns the full-lake audit |
| `cne config` | none | create writes the personal config; upgrade backs up, then adds schedule steps; validate/diff only read |
| `cne contract diff` | none | Reads contracts and prints the diff |
| `cne contract show` | none | Reads the contract; writes a file when an output path is given |
| `cne contract validate` | none | Validates the contract |
| `cne decision-data cash-rights` | none | Reads the lake and prints decision data |
| `cne decision-data payment-gaps` | none | Reads the lake and prints decision data |
| `cne decision-data stock-terms` | none | Reads the lake and prints decision data |
| `cne delisted status` | none | Reads delisting status |
| `cne derive` | conditional | Writes derived data; modes such as adj_factors may access sources |
| `cne doctor` | none | Checks config and environment offline |
| `cne init` | conditional | sample is offline; demo/quick/full fetch data; layout-only creates directories |
| `cne mcp` | conditional | Reads the lake by default; --live may fetch source data; --http listens on a local port |
| `cne profile list` | none | Lists built-in scopes |
| `cne profile show` | none | Shows a built-in scope |
| `cne query` | conditional | SQL reads the lake; fetches data on an on-demand cache miss or refresh |
| `cne repair adj-factors` | --refresh-sources only | Offline preview with a saved plan by default; --apply publishes supported corrections, retains old revisions and unresolved diagnostics, and saves bounded historical progress |
| `cne repair corporate-action-gaps` | with --apply | Offline preview by default; --apply queries Baostock and CNINFO per gap, publishes a new version after verification and keeps the old one |
| `cne repair daily-bar-coverage` | none | Read-only preview by default; --apply retires BSE session debts and proven pre-listing debts using committed data and refreshes coverage watermarks, retaining evidence receipts without changing bars |
| `cne repair financial-announcements` | none | Read-only preview by default; --apply appends financial reconstructions with LICO original-disclosure evidence, publishes through the repair gate and retains existing vintages |
| `cne repair financial-originals` | --refresh-sources only | Replays original periodic reports offline by default; --apply appends identifiable current-period consolidated figures, publishes through the repair gate and retains existing vintages and honest observation times |
| `cne repair fund-factors` | none | Read-only preview by default; --apply overlays recorded fund splits and transfers on the original vendor series, publishes through the repair gate and retains the old revision |
| `cne repair fund-listing-dates` | Requests current official ETF/LOF directories | Preview by default; --apply archives source responses and fills unknown listing dates through the repair gate, retaining known dates and old revisions |
| `cne repair fund-no-print` | --refresh-sources only | Offline preview by default; source refresh archives reports and updates caches; --apply approves explicit official fund auction-bar coverage evidence, retires corresponding debts and refreshes watermarks without creating bars or halt rows |
| `cne repair fund-volume` | only with --refresh-sources | Offline preview by default; online refresh archives historical K-lines from the SSE quote service and reuses or collects EastMoney evidence when turnover conflicts; --apply repairs verified Shanghai and Shenzhen fund volume, passes publication checks and retains old revisions |
| `cne repair index-history` | --refresh-sources only | Replays the reviewed official attachment catalog for the selected SZSE Component or STAR 50 index offline by default; --apply replaces whole books on effective dates, retaining official dated weights and old revisions; unknown historical weights remain null |
| `cne repair index-snapshots` | --refresh-sources only | Replays official files offline by default; --apply replaces complete dated constituent snapshots and real weights, publishes through the repair gate and retains old revisions |
| `cne repair layout` | none | Preview only by default; --apply publishes a new version, keeping the old version and duplicate-observation evidence |
| `cne repair misdated-valuation` | with --apply | Offline preview by default; --apply fetches valuation for the replaced sessions from the EastMoney datacenter, publishes a new version after verification and keeps the old one |
| `cne repair missing-values` | none | Read-only preview by default; --apply replaces valuation zero placeholders and unavailable EastMoney/CNI weights with nulls, publishes through the repair gate and retains the old revision |
| `cne repair orphan-symbols` | none | Preview by default, may register an old-lake baseline version; --apply removes factor and corporate-action rows of securities without daily bars, publishes a new version and keeps the old one |
| `cne repair stale-suspensions` | none | Preview by default, may register an old-lake baseline version; --apply removes inferred suspensions contradicted by traded daily bars, publishes a new version and keeps the old one |
| `cne repair status-listing-window` | none | Preview by default, may register an old-lake baseline version; --apply deletes status rows before a security's listing date or without a delisting on record, publishes a new version and keeps the old one |
| `cne repair status-outages` | none | Preview by default, may register an old-lake baseline version; --apply rebuilds the EastMoney-outage carried rows in the window under the current rule, publishes a new version and keeps the old one |
| `cne repair status-traded` | none | Read-only preview by default; --apply fills missing status keys from committed positive-volume bars, leaves ST unknown, publishes a new version and keeps the old one |
| `cne repair stock-listing-dates` | --refresh-sources only | Replays IPO calendars offline by default; --apply fills stock listing dates confirmed by official listed events, publishes through the repair gate and retains old revisions |
| `cne repair valuation-basis` | none | Preview only by default; --apply publishes a new version and keeps the old one |
| `cne run clean` | none | Only previews expired files; an explicit --reconcile-runs may modify run state |
| `cne run compact` | none | Publishes finished, unpublished staging data to the lake |
| `cne run daily` | when run | Fetches incrementally and writes the lake; includes all schedule groups and event streams by default |
| `cne run events` | when run | Fetches events and writes the lake |
| `cne run retry` | when run | Retries failed scopes and writes the lake |
| `cne serve` | conditional | Starts the lake dashboard. On a loopback address the operations page can start allowlisted fetches and daily updates; --read-only disables write endpoints; remote fetches require --allow-remote-ops; storage cleanup still needs web confirmation; fetch toggles are written back to the config, with a backup, after preview confirmation and do not start a fetch |
| `cne snapshot create` | none | Creates a local snapshot |
| `cne snapshot delta apply` | none | Applies a local delta package; --dry-run only validates |
| `cne snapshot delta create` | none | Creates a local delta package |
| `cne snapshot delta verify` | none | Verifies a local delta package |
| `cne snapshot export` | none | Exports a local snapshot |
| `cne snapshot import` | none | Imports a local snapshot |
| `cne snapshot restore` | none | Restores a local snapshot to the target directory |
| `cne snapshot verify` | none | Verifies a local snapshot |
| `cne sources limits` | none | Reads egress budgets, cooldowns and local debt; does not probe sources |
| `cne sources policy` | none | Reads source policies |
| `cne sources probe` | conditional | --list is offline; probing accesses sources and writes a report |
| `cne sources resilience` | none | Reads registered sources and probe evidence; may write a report |
| `cne sources slo` | none | Reads probe history and writes stats |
| `cne sources substitutes` | conditional | Reads reports by default; --probe accesses sources |
| `cne stats rebuild` | none | Rebuilds local stats |
| `cne stats show` | none | Reads local stats; recomputes automatically when stale or when a detail view needs it |
| `cne status` | none | Reads run state and dataset coverage |
| `cne storage apply` | none | Rechecks the version plan and marks; CLI purge is disabled, deletion must be confirmed in the serve web UI |
| `cne storage explain` | none | Reads the retention reasons for versions |
| `cne storage hold` | none | Adds object protection and clears pending-deletion marks |
| `cne storage import` | none | Validates and registers a reference manifest; only adds protection |
| `cne storage inspect` | none | Reads versions and retention reasons |
| `cne storage plan` | none | Validates retention dependencies and saves a version plan; deletes no data |
| `cne storage raw-external` | none | Registers external payload storage; retains local source sidecars and does not move or delete files |
| `cne ths-official backfill` | when run | Calls the credentialed source and fills gaps |
| `cne ths-official capture` | when run | Calls the credentialed source and writes comparison evidence |
| `cne ths-official repair-bars` | when run | Fetches even in a dry run; --apply writes repairs |
| `cne ths-official resource-sectors` | when run | Fetches even in a dry run; --apply writes staging |
| `cne verify` | conditional | Local validation by default; --repair accesses sources and repairs |
