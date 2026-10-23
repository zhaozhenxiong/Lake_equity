# Institutional holdings ratio unit

## Change

Schema version 2 declares `holding_ratio` as `percent`. The EastMoney adapter
and stored values already use percentage points: `8.5` means 8.5%, rather than
a fraction of 8.5. This corrects the metadata; the physical column and values
are unchanged.

## Migration

Do not rescale existing lake values. Consumers that need fractions should
divide `holding_ratio` by 100 once. Refresh cached unit metadata and saved
contract fingerprints, and record schema version 2 for new research receipts.
Inspect separately any manually imported data that followed the old fraction
declaration.

## Rollback

Retain the previous software and contract snapshot for replay. Reverting
software requires no lake rewrite, but the old fraction label is incorrect for
EastMoney values; use an explicit percentage interpretation in that reader.
