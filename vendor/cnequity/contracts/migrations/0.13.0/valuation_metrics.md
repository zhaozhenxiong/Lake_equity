# valuation_metrics — additive columns, corrected field meaning

## Change

Four nullable columns are added: `pe_dynamic`, `total_mv_basis`,
`float_mv_basis` and `shares_as_of`. The schema version stays 1.

What changes is the meaning of values that were stored under one name:

- `pe_ttm` from EastMoney push2 (`source = "eastmoney"`) was the dynamic P/E
  (`f9`, latest report annualised). It now lands in `pe_dynamic`, and
  `pe_ttm` holds only trailing-twelve-month P/E.
- baostock `float_mv` was `amount / (turn / 100)`, the turnover-implied float
  priced at the session VWAP. It is now `close × volume / (turn / 100)`.
- baostock `total_mv` used the previous year-end share count. It is now
  `close × total shares effective that session` from `share_structure` when
  the lake has that history, and is labelled as a year-end estimate otherwise.

Every stored market cap names its basis in `*_mv_basis`; vendor values are
`vendor_reported`.

## Migration

New ingestion writes the new form. For stored rows run, once:

```bash
cne repair valuation-basis            # plan: counts per basis, nothing written
cne repair valuation-basis --apply    # publish one repaired revision
```

The repair reads only the lake. A legacy `float_mv` whose bar cannot confirm
its VWAP (missing bar, or VWAP outside the bar's low..high) keeps its value
with basis `vwap_x_turn_implied_shares`. Readers that need one definition
filter on the basis columns instead of on `source`.

## Rollback

The repair publishes a new revision and retains the previous one;
`load("valuation_metrics", revision=<previous>)` reads the old values. Code
rollback restores the old adapters; the added columns are then ignored by
older readers.
