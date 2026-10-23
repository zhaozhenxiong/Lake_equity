# From collection to query

```text
Configure collection scope → Fetch data → Validate and stage → Merge and publish a data version → Query and check quality
```

## Collection and updates

`cne init` builds the initial data, and `cne run daily` updates the daily-update data and runs the event stream at the end (announcements and news; `cne run events` can also be run on its own). `cne backfill` only applies to datasets that support historical backfill. Snapshot sources cannot automatically provide complete past history.

## Staging and publishing

Unfinished batches stay in staging for retries. Batches that have not yet been validated and sealed cannot be merged into a publish; the valid portion that has already been sealed can be published, and the gaps stay in the ledger. Other datasets may already have been updated. Published data versions are never modified in place; corrections produce a new version.

## Dashboard

`cne serve` shows published data, watermarks, and tasks. On a loopback address, the operations page starts allowlisted initialization, daily updates, catch-up fetches, inspections, and data backups; the runs page can also start, by hand, the commands that leave a run record (daily update, a schedule group, catch-up, event streams, backfill, re-derive), through the same preview form. `--read-only` turns off these write entry points. Remote fetching also requires `--allow-remote-ops`. MCP does not provide these operations. For the steps, see the [Quickstart](../getting-started/quickstart.md#browser) and the [Runbook](../operations/runbook.md).

## Query and checks

The Python API and SQL read published data. Use the corresponding query options when you need price adjustment, historical universes, or PIT filtering. The raw `scan()` does not apply these research filters automatically.

Data freshness, coverage completeness, and semantic correctness need to be checked separately. Post-publish audits can mark a run as failed, but they do not automatically withdraw data that has already been published; for configuring pre-publish gates, see the [configuration reference](../getting-started/configuration.md).

## Failure recovery

First look at the run status and findings, then follow the [troubleshooting guide](../operations/troubleshooting.md) to choose between retrying, filling in data, or repairing. Do not clear alerts by deleting the lake directory or manually editing published files.
