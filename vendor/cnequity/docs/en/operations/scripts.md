# Repository scripts

Path: `scripts/`

| Directory | Contents |
|---|---|
| `scripts/scheduler/` | Scheduling: daily / event stream / catch-up pipelines, health notifications, metadata backup, launchd templates and installation (rerun `install_scheduler.sh` after changing paths, since installed jobs reference absolute paths) |
| `scripts/` | Operations tools: backfill acceptance, delisted catalog rebuild, catch-up runs, survivorship bias measurement |
| `scripts/migrations/` | One-off data migrations and partition rewrites, run according to the version notes |
| `scripts/dev/` | Repository maintenance: docs sync (`sync_*`), release checks, offline benchmarks and end-to-end checks, probes |

These scripts need a source checkout and are not installed with the PyPI package. With the installed package only, schedule `cne run daily` once a day (it includes all daily groups and the event stream); in the repository, `daily_pipeline.sh` is the main entry point for the daily update.

> The scripts below target **self-hosted machines/VPS** (including macOS launchd and server backfills). Open-source contributors only need
> the CLI (`cne init` / `run`); scheduling and alerting are optional and not the only way to deploy.

## Production scripts

### daily_pipeline.sh

**Purpose**: on trading days, run all schedule groups serially, ending with a health check and a backup.

**Flow**:

```
for group in $(python -m cnequity.orchestrator.schedule_groups --config "$CNE_CONFIG"); do
  cne run daily --group $group
done
health_notify.sh
cne sources probe --stale-only --vantage $CNE_SOURCE_VANTAGE
cne sources slo             # cumulative report, not enforced by the daily update
cne verify --runs --days 20 # cumulative report, not enforced by the daily update
backup_meta.sh
cne run clean
```

In a later window, the separate `stale_pipeline.sh` catches up only the snapshot datasets that are still behind, sharing the scheduler lock with the main daily update. The lock lives in `{data.root}/locks` and is the same lock used by the `cne serve` operations page; `CNE_SCHEDULER_LOCK_DIR` or `CNE_LOCK_DIR` changes the directory. The lock records the host, PID and process start identity. Verified dead local owners are reclaimed automatically; a legacy PID-only marker is reclaimed once that process has exited, and a lock directory with no marker after 60 seconds. Remote owners require maintainer verification before removal. The main script keeps the old in-process wait switch for compatibility, but it is off by default. See [runbook · closing catch-up](runbook.md#closing-catch-up).

**Environment variables** (`CNE_CONFIG` / `CNE_LOG_DIR` are also read by `cne` itself; the rest are read only by this script): `CNE_CONFIG`, `CNE_LOG_DIR` (long-running commands also write their own log here), `CNE_GROUPS`,
`CNE_GATE_GROUPS` (default `core`; failures are marked gate, other groups are marked soft),
`CNE_SOFT_FAIL_OK` (default `1`: soft failures exit 0 when the gate is OK; `0` = still exit 1),
`CNE_STALE_RETRY` (default `0`; `1` enables the old compatibility catch-up),
`CNE_STALE_RETRY_DELAY_SEC` (default `1800`),
`CNE_SOURCE_HEALTH` (default `1`; `0` turns off the daily stale-evidence probe),
`CNE_SOURCE_VANTAGE` (default `local`; set it to a stable, truthful egress label such as `cn` or
`overseas`),
`CNE_TRADE_DATE`,
`CNE_BIN` (overrides the `cne` path; used by tests that exercise the control flow with a stub),
`CNE_PYTHON` (interpreter for the gate and the scheduler lock; defaults to the repository's `.venv/bin/python`, or the `python3` on PATH when there is no `.venv`; it must be able to import cnequity).

At the end it prints a per-group summary (`group: OK|FAILED [gate|soft]`) plus `stale-retry: OK|FAILED|not needed|skipped`, making it easy to tell "the gate broke" from "EastMoney broke". A catch-up failure counts as soft.

### events_pipeline.sh

**Purpose**: the 24/7 event stream (announcements, regulatory events, news). `cne run events` runs all groups in
`[job.events.groups]` at once, or a single one set with `CNE_EVENTS_GROUP`.

**Why it is separate from `daily_pipeline.sh`:**

- The repository's daily script calls `cne run daily --group` per group, which does not include the event stream. `cne run daily` without arguments runs the event stream itself after the schedule groups; on non-trading days the schedule groups are skipped and the event stream still runs.
- The event stream uses a separate shell lock (`events`) and the `events_ingestion` lock, so it can overlap with the per-group daily update. The datasets the two write are disjoint, which config validation guarantees.
- Announcements and news are also published on weekends and holidays, so this job is scheduled by calendar day and ignores the trading calendar.

**Environment variables**: `CNE_CONFIG`, `CNE_LOG_DIR`, `CNE_BIN`, `CNE_TRADE_DATE`,
`CNE_EVENTS_GROUP` (default all groups), `CNE_SCHEDULER_LOCK_DIR`.

If the previous run is still going, it skips and exits 0: every group rereads its own window next time, so skipping once loses no data.

### stale_pipeline.sh

**Purpose**: a later stale-only catch-up, `cne run daily --stale-only`.

**Why it is a separate job**: the normal six-group daily update must **finish and report on time** even when a source is down; stuffing a 30-minute wait into that process would make a source outage look like a scheduler timeout of several hours. A second, later window can still fill the snapshot-only datasets without holding up the normal run.

**Mutually exclusive with the daily update**: it shares the same lock from `scheduler_lock.sh` with `daily_pipeline.sh`. launchd may trigger it while the main pipeline is still running — in that case it **skips** (exit 0) rather than queueing, because the next window can try again, and duplicate runs must never pile up behind ingestion.

Environment variables: `CNE_BIN` / `CNE_CONFIG` / `CNE_LOG_DIR`, optionally `CNE_STALE_GROUPS`, `CNE_TRADE_DATE`, `CNE_PYTHON` (as above).

### scheduler_lock.sh

The launchd/cron-level mutex shared by the three shell jobs: daily, event stream and stale.

macOS does not ship `flock`, and the Python run lock only covers a single `cne` invocation — the daily script runs several commands, so it needs a lock that covers the whole script. It is sourced by `daily_pipeline.sh` and `stale_pipeline.sh`.

### scheduler_config.py

Renders and diffs launchd jobs, **preserving the scheduling choices already on this machine**. `install_scheduler.sh` uses it to generate the three plists from one config instead of hand-writing each.

### install_scheduler.sh / uninstall_scheduler.sh

Generates the user launchd plist from `scripts/scheduler/launchd/com.cnequity.daily.plist.template` and loads `daily_pipeline.sh`.
It also installs `com.cnequity.stale` (closing catch-up) and `com.cnequity.events` (event stream, 14:00 every calendar day in the machine's local time zone, not Beijing time; adjust to your own disclosure cadence).
At install time, use `CNE_SOURCE_VANTAGE=cn scripts/scheduler/install_scheduler.sh` to pin a truthful egress label; when omitted,
`local` is used. The label may only contain letters, digits, dots, underscores and hyphens, to avoid generating an invalid plist.

### health_notify.sh

```bash
# Weekdays
cne audit                 # only the active partitions of this run
cne status --datasets --gate --groups "$CNE_GROUPS"
# Once a week (Saturday by default)
cne audit --full          # full-lake structural scan + refresh health-latest.json
```

On failure it sends a macOS `osascript` notification and exits non-zero. The notification title depends on what failed:
the audit/health branch is "数据异常" (data anomaly), and a pure freshness lag is "数据滞后" (data lagging).

**Why they are split**: `cne audit --full` scans the whole lake, and its I/O cost grows with the number of files. Day to day, audit per run first and check the whole lake weekly; record actual run times in your own operations log.

**Environment variables**: `CNE_FULL_AUDIT_DOW` (default `6` = Saturday, `1-7` map to Monday through Sunday;
`0` turns off the full-lake audit entirely; `always` restores the old behavior of running it every day).

**Why the freshness gate is narrowed by group**: `CNE_GROUPS` is passed as-is to `cne status --datasets --gate --groups`,
i.e. the groups this host actually runs. Without narrowing, on a host that only schedules `core`, twenty-odd datasets
**have no job fetching them** and would show STALE indefinitely, masking real anomalies.
The gate and the scheduler share the same variable, so the two never disagree. Datasets nobody schedules still fail:
"not knowing who fetches it" is not the same as "another host fetches it".

> The partition fragmentation / mixed granularity checks in the full-lake audit only change after a repartition,
> so they were moved to the weekly run as well. After running `scripts/migrations/repartition.py` manually,
> run `cne audit --full` right away to double-check instead of waiting for Saturday.

### backup_meta.sh

Packs `meta/manifest.db`, `state/`, `quality/`, revision **receipts**,
`source_snapshots/`, `source_health/` and `stability/` into
`meta-YYYYMMDD-HHMMSS.tar.gz`. It does not include the data version files in `meta/revisions/data/` or
`curated/`; it is a metadata backup and cannot restore the full data lake on its own.

Arguments: `backup_meta.sh [DATA_ROOT] [BACKUP_DIR] [RETENTION_DAYS] [RETENTION_COUNT]`.
Without `DATA_ROOT` it uses `CNE_DATA_ROOT`, otherwise the repository's `data/cnequity`;
it **does not** read the lake path from `CNE_CONFIG`. By default it keeps 14 days and at most 30 copies;
the backup directory defaults to inside the lake, so set `CNE_BACKUP_DIR` outside the lake when you need disk-level disaster recovery.
The runtime environment should have the `sqlite3` command installed so the script can copy the running manifest with the SQLite backup API;
without it the script falls back to a file copy, and backups should be taken after ingestion has stopped.

### run_catchup.py

**Purpose**: bring the gate up to date after a missed run / a weekend — `daily:core`, then `market_breadth` + `compact`
(the latter skipped with `--core-only`). It does not pass `--backfill` (a full CA scan is fragile over an overseas egress).

```bash
python scripts/run_catchup.py                          # latest trading day
python scripts/run_catchup.py --trade-date 2026-07-17
python scripts/run_catchup.py --trade-date 2026-07-17 --all-groups
```

**The exit code follows the gate, not the extra groups**: a core or market_breadth failure exits 1; failures of groups in `--extra-group` /
`--all-groups` are only reported and do not change the exit code — the EastMoney-heavy groups are flaky over an overseas egress anyway.
Parts whose watermark has already reached the target day are marked `skipped_already_fresh` and not rerun.

Formerly `cne run catchup`. It is orchestration rather than a capability (each step is `cne run daily` on one schedule
group), the same kind of thing as `daily_pipeline.sh`, so it lives here rather than in the published CLI.

## Init and acceptance

Historical initialization and resume use the public CLI directly:

```bash
cne init --profile full --since 2016-01-01   # after an interruption, rerun the same command to resume
```

For source capability diagnostics, start with `cne sources probe --list` to pick the smallest probe; before a backfill, check the range and request estimate from
`cne backfill <dataset> --plan`.

### accept_backfill.py

Backfill acceptance tool:

```bash
python scripts/accept_backfill.py snapshot --out /tmp/counts.json
python scripts/accept_backfill.py check --compare /tmp/counts.json
```

Checks idempotency and the stability of curated row counts.

### delisted_ops.py

Four subcommands for rebuilding the delisted universe (survivorship bias repair). **Reading** the catalog and fetching quotes stay in the CLI
(`cne delisted status` / `cne backfill daily_bars --profile delisted`) — those two have a day-to-day form; these four do not.

| Subcommand | Description |
|--------|------|
| `discover [--limit N]` | Scans the issued code space and classifies codes as once listed / never issued; resumable, and codes whose probe failed stay pending rather than being recorded as "never issued" |
| `reconcile [--apply]` | Read-only check of catalog end dates by default; `--apply` only corrects end dates disproved jointly by the formal delisting date, the trading calendar and positive-volume quotes, and writes a backup and a SHA-256 receipt |
| `repair [--since]` | **Does not refetch quotes**: writes `instruments.delist_date` from the existing `daily_bars` span and clears the `认购款` (subscription payment) placeholders |
| `coverage [--start] [--end]` | **Read-only strict gate**: verifies discovery completeness, window overlap, the last valid trade and instruments identity; exits 1 if it does not pass |

```bash
cne delisted status                                   # how many are known
python scripts/delisted_ops.py discover --limit 500
cne backfill daily_bars --profile delisted --start 2016-01-01
python scripts/delisted_ops.py repair
python scripts/delisted_ops.py reconcile
python scripts/delisted_ops.py reconcile --apply
python scripts/delisted_ops.py coverage --start 2016-01-01 --universe all_a_sh_sz
```

Only `coverage` is a gate: it exits 1 when verification does not pass, so any workflow claiming to be "survivorship-safe" should pass it first.

The pass claim of `coverage` is deliberately narrow: it proves that the delisted catalog has been fully scanned and that the known delisted securities overlapping the window have a consistent
last valid trade and security master data; it does not prove that every trading day between the two ends is continuous. Sources may keep
zero-volume placeholder rows before a suspension or formal delisting, and the gate does not mistake them for the last trade. Securities whose catalog end date is after the window but which have no quotes in the window proving they were
listed go into `unknown_overlap` by default. When the complete Baostock security master explicitly gives an IPO after the window, they are listed as `not_yet_listed`; when every expected trading day in the complete trading calendar has independent Baostock suspension evidence, they are listed as `verified_nontrading`. Derived quote gaps, calendars missing dates or incomplete suspension records cannot serve as grounds for passing; both categories are kept item by item in the report. Before a security identity record is updated, a content-addressed historical copy is kept.

`reconcile --apply` does not take the "last record" returned by a single vendor as the truth: automatic changes are allowed only when there is a curated positive-volume end point,
that end point is no later than `instruments.delist_date`, and the old catalog date also falls after the formal delisting date or on a non-trading day.
It refuses to run if any active ingestion run is detected; the catalog before the change is saved in
`meta/state/history/`, and the quality receipt is written to `meta/quality/`.

Formerly `cne delisted discover / reconcile / repair / coverage`.

## One-off migrations

### repartition.py

Rewrites historical partitions into the period configured in `DatasetSpec.partition_granularity`
(see [partition granularity](../architecture/lake-layout.md#day-to-day-management)). Without arguments it only lists the datasets awaiting a rewrite,
so the listing form can be run at any time.

```bash
python scripts/migrations/repartition.py                  # datasets awaiting a rewrite
python scripts/migrations/repartition.py --all --dry-run  # preview the impact first
python scripts/migrations/repartition.py trading_calendar # a single dataset
```

The read path resolves the layout from the directory shape, so changing the granularity itself **does not need** a migration; this only gathers up fragmented files. Writes first build a temporary directory,
write each partition and check the total row count, then swap it in with a single rename, so a crash midway leaves the original data untouched; repeated runs are idempotent.

Formerly `cne repartition`. What triggers it is a change of registry granularity under an existing lake — that is a migration, not routine operations.

### migrate_daily_bars_volume_v2.py

Rewrites all of curated `daily_bars.volume` to "shares" and bumps `data_version` from `v1` to `v2`.

Before the fix, this column mixed two units: `tdx_protocol` / `sina` wrote lots, `ths` / `baostock`
wrote shares, exactly 100 times apart. Existing rows were wrong under either convention and could only be rewritten. For the unit contract and migration requirements, see
[Schema contract · volume units](../datasets/schema.md).

```bash
scripts/migrations/migrate_daily_bars_volume_v2.py --config configs/cnequity.toml --dry-run
scripts/migrations/migrate_daily_bars_volume_v2.py --config configs/cnequity.toml --apply
```

- `--dry-run` (the default) only counts and writes nothing; `--apply` **rewrites curated in place**, so run `backup_meta.sh` and back up curated first.
- Idempotent: rows already at `v2` are skipped, and after an interruption you can simply resume.
- `fetched_at` is not restamped — the column that records this reinterpretation is `data_version`.
- Afterwards, run `cne audit` to confirm there is no `daily_bars_volume_unit` finding.

## Tests and smoke runs

### smoke_daily_e2e.py

End-to-end smoke test: runs a miniature daily path with mocks or a lightweight config, for CI/local regression.

## Docs sync and translation

### dev/sync_docs.py

Regenerates the parts of the docs that code decides, in both languages: the field tables, the derivatives capability table, the CLI options table, the command side-effect inventory, and the README used on PyPI. `--check` only checks and writes nothing; CI runs it. CLI help in both languages comes from the package catalog (English in the code, Chinese in `src/cnequity/locale/zh_CN/LC_MESSAGES/cnequity.po`), and `python scripts/dev/i18n_catalog.py` fails on help with no Chinese; the English for side-effect notes lives in `scripts/dev/i18n/cli_surface.en.yml`, and adding a command without its English fails the check.

### dev/i18n_catalog.py

Maintains the translation catalog for the CLI, terminal progress and the console's server-side text. Text is written in English in the code and marked with `_()` (translated at run time) or `N_()` (only registered, translated when shown, as for Click help and log templates) from `cnequity.i18n`; a command's docstring is its help and is registered automatically. The Chinese lives in `src/cnequity/locale/zh_CN/LC_MESSAGES/cnequity.po`, and the compiled `.mo` is committed with it, so installing needs no build step:

```bash
python scripts/dev/i18n_catalog.py update    # add new text, drop text no longer in the code
python scripts/dev/i18n_catalog.py compile   # rebuild the .mo from the .po
python scripts/dev/i18n_catalog.py           # check; exits 1 on any problem
```

The check covers: marked text with no entry or no Chinese, entries no longer in the code, fuzzy entries, placeholders that differ from the English, leading or trailing newlines that differ, and a `.mo` that does not match its `.po`. `tests/unit/test_i18n_catalog.py` runs it, and `tests/unit/test_cli_untranslated.py` also catches Chinese literals that bypass the catalog. Babel is a development dependency only; the installed package reads the `.mo` with the standard library.

### dev/doc_translations.py

Lists English pages that are behind their Chinese originals. Each English page records, in `scripts/dev/i18n/doc_sources.json`, a digest of the Chinese page it was translated from; when the Chinese changes and the English does not follow, `tests/unit/test_docs_i18n.py` fails. After updating the English, mark it:

```bash
python scripts/dev/doc_translations.py --mark reference/mcp.md
```

Generated tables are left out of the digest, so regenerating a table never asks for a new translation.

## launchd templates

`scripts/scheduler/launchd/com.cnequity.daily.plist.template`

- `ProgramArguments` points to `daily_pipeline.sh`
- `StartCalendarInterval`: Minute=7 (wakes every hour); `CNE_SCHEDULED=1` makes `daily_pipeline.sh` ask `scheduler_gate.py` first, so it runs only once per trading day after `[job.daily] run_at` Beijing time
- Standard output/error are redirected to `{data.root}/logs/launchd.*.log`

`scripts/scheduler/launchd/com.cnequity.stale.plist.template`

- `ProgramArguments` points to `stale_pipeline.sh`
- `StartCalendarInterval`: Minute=37 (wakes every hour); runs once only after that day's daily update has run and `[job.stale] run_at` Beijing time has passed, catching up only snapshot datasets (`--stale-only --snapshots-only`)
- Shares the `scheduler_lock.sh` lock with the daily update; skips while the main pipeline is still running

`scripts/scheduler/launchd/com.cnequity.events.plist.template`

- `ProgramArguments` points to `events_pipeline.sh`
- `StartCalendarInterval`: Hour=14, Minute=0, **without `Weekday`** — the event stream is meant to run on
  weekends and holidays (about 20:00 CST on a UTC+2/+3 machine)
- For intraday news freshness, add another high-frequency agent/cron with `CNE_EVENTS_GROUP=news_wire`;
  `disclosures` rereads a 30-day trailing window each time and is not suited to high frequency

## Related docs

- [Operations runbook](runbook.md)
- [Quickstart](../getting-started/quickstart.md)
