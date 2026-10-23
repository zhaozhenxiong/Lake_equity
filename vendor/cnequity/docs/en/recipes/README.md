# Research recipes: choose an entry point by question

Prepare the corresponding data first, then run the queries. The basic demo contains only a small range of market data; it cannot be used directly for financial statement, full-market universe or derivatives research.

| What you want to verify | Recipe | Prerequisites |
|---|---|---|
| The difference between unadjusted and hfq returns | [Price adjustment research baseline](research-baseline.md) | `demo --research`; requires TDX and Sina |
| The financial statement version visible on a rebalance date | [PIT financial statement cross-section](pit-rebalance.md) | A production lake with financial statements and contemporaneous observation evidence; a fresh backfill may yield no strict results |
| SQL aggregation and Polars feature processing | [DuckDB and Polars](duckdb-polars.md) | The relevant data has been compacted; the price adjustment example also needs factors |
| Term structure, option chains and Greeks | [Commodity futures and options](derivatives.md) | Explicitly enabled, backfilled per exchange and verified |
| Computing 15 / 30 / 60-minute bars on the fly and storing them in the lake | [15 / 30 / 60-minute bars](minute-bars-15-30-60.md) | 1m or 5m enabled and backfilled |

## Conventions shared by all recipes

1. Use the configuration each page specifies. Across working directories or with MCP, use an absolute config path and an absolute `data.root`.
2. Price adjustment research uses `strict_adj=True`; PIT uses an explicit `pit_mode="strict"`; for universes, choose a versioned profile or `strict_universe=True`.
3. The coverage bounds from `list_datasets()` are a starting point for finding data; they do not prove there are no gaps inside the window. Judge with the actual dates, quality reports and research gates.
4. Pin dependency revisions, query parameters, software version and the necessary evidence. Use research snapshots for long-term retention; the recipes are not promises of backtest returns or investment performance.

For API details, see the [query guide](../datasets/query-guide.md) and the [Python API](../reference/python-api.md).
