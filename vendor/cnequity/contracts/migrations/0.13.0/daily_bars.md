# daily_bars / index_bars — additive `pre_close`

## Change

A nullable `pre_close` (float64) column is added to `daily_bars` and
`index_bars`. It is the previous close the exchange published for the
session. On an ex-rights date that value is the ex-rights reference price.
The schema version stays 1.

Sources that publish it fill it from the day this release is installed:
the SSE and SZSE boards, the BSE quotation board and TDX real-time quotes.
K-line history (TDX history, Sina, Baostock, THS) does not carry it, so those
rows and every stored row read as null.

## Migration

Nothing needs to be run. Stored partitions without the column read as null,
and readers that select columns explicitly keep working.
