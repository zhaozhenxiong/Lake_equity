# block_trades — schema_version 2

## Change

The contract declared `volume` in shares and `amount` in CNY. The stored rows
have always carried EastMoney's report scale, 万股 and 万元 (10,000 shares and
10,000 CNY); the exchange backup keeps that scale on purpose. The declaration
now reads `10k_share` / `10k_CNY`. No stored value changes.

## Migration

Readers that multiplied `volume` or `amount` by the declared unit must scale by
10,000 to get shares or CNY. Readers that already treated the columns as
万股/万元 need no change.

## Rollback

Restore `schema_version=1` and the `share` / `CNY` unit strings on the spec and
regenerate the contract. The data is untouched either way.
