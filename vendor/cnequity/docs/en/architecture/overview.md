# Product direction and scope

CNEquity is for users who want to manage their own China-market research data: collect continuously on their own machine, query with consistent data semantics, and be able to find gaps, trace sources, and recover failed tasks. The maintainers plan the project's direction and feature scope; users can report scenarios, problems, and suggestions through Issues.

## Core capabilities

- Local data lake: data is stored as Parquet and read through Python, DuckDB, Polars, the dashboard, or read-only MCP.
- Continuous updates: initialization, daily updates, event collection, and historical backfill use explicit configuration and scope.
- Visible quality: sources, units, coverage, and failure status can be inspected; freshness does not mean the history is complete.
- Traceable versions: corrections are published as new data versions; when you need long-term reproducibility, save a research snapshot, the query parameters, and the software version.

## Limits to know before you start

The installation does not ship with market data, and collection depends on upstream availability and permissions. Optional credentialed sources must be configured by you. For the currently supported scope, see the [dataset catalog](../datasets/catalog.md) and [data source limitations](../datasets/sources.md).

Historical backfill cannot prove that the data was observed at the time. Strict research should explicitly choose PIT, universe, and price-adjustment checks; insufficient evidence can cause a query to refuse to return results. See the [query guide](../datasets/query-guide.md).

Datasets are published separately; there are no cross-table atomic transactions across the whole lake. For the boundaries of pinned-version reads, version retention, and snapshots, see the [Python API](../reference/python-api.md).

## Where to start

[Quickstart](../getting-started/quickstart.md) → [Initialization and resume](../getting-started/initialization.md) → [Runbook](../operations/runbook.md). When something goes wrong, first use [Troubleshooting](../operations/troubleshooting.md) to narrow down the scope of the failure.

For the product data flow, see [From collection to query](data-flow.md); for managing local files, see [Data lake layout](lake-layout.md).

## Is it right for me? {#is-it-right-for-me}

CNEquity suits data work that accumulates continuously, queries repeatedly, and re-checks historical semantics. When choosing, first decide whether you need a one-off fetch, a long-term data layer, or a research and trading application.

### Choose by way of working

| Need | Suitable way of working | Where CNEquity fits |
|---|---|---|
| Occasionally fetch a quote or a table | Use the API or data-fetching library of the source you need directly | The cost of building a lake may outweigh the benefit |
| Reuse the same historical data across many studies | Persistent local storage, incremental collection, versions, and quality checks | This is the project's main purpose |
| Get data with explicit licensing or a service commitment | Choose a data service according to the vendor contract | Collecting from public sources cannot replace a license or an SLA |
| Model training, backtesting, or trade execution | Use the corresponding research / trading framework | The lake can serve as its input; integration and strategy logic are the downstream's responsibility |
| Let an agent query historical data | Prepare the data and evidence first, then expose read-only tools | `cne mcp` provides read-only tools (stdio or HTTP) |

Names such as AkShare, efinance, Tushare, Baostock, Qlib, and vn.py often come up in these workflows; this page does not give static ratings of their current features, pricing, or licensing. When choosing, rely on the actual interfaces, versions, and terms of the respective projects and services. For how CNEquity differs from AKShare and Tushare in way of working, and how to use them together, see the [comparison](comparison.md).

### What CNEquity already takes care of

- Unified datasets, primary keys, units, and source columns, stored as open Parquet.
- Batch-based collection, watermark maintenance, retained failure ranges, and resume support.
- Separate quality checks for candidate data and published data; publishing a new revision keeps the evidence of the revision.
- Price adjustment, historical universe, and PIT query semantics; strict mode exposes gaps explicitly.
- Python / SQL, dashboard, and MCP entry points. On a loopback address, the dashboard can preview and start allowlisted initialization, daily updates, catch-up fetches, inspections, and backups; MCP remains read-only.

### Costs and limits

You take on the disk, run scheduling, and upstream reachability yourself. Snapshot data can only accumulate from the time you enable it; historical backfill does not automatically carry strict PIT evidence. Version retention is capped, and long-term reproduction requires saving snapshots, dependencies, and the software version.

The project does not provide hosted market data, a backtesting engine, trading signals, or order placement. The code license does not grant redistribution rights to upstream data; see [Data licensing](../legal-and-data-sources.md).

Start by building a trial lake with the [Quickstart](../getting-started/quickstart.md), then assess the collection scope with the [initialization guide](../getting-started/initialization.md).
