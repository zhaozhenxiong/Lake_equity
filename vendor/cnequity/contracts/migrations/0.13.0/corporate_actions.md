# corporate_actions — reorg_transfer and reference_price

## Change

One nullable column, `reference_price` (CNY/share), and one action type,
`reorg_transfer`, are added. The schema version stays 1 and no existing
field changes meaning.

A court-approved restructuring converts capital reserve into new shares
that go mostly to creditors and investors. `transfer_ratio` still counts new
shares per held share, so share accounting is unchanged. The price effect
does not follow `1 + transfer_ratio`: the issuer publishes an ex-rights
reference price, and the day's factor step is `previous close ÷
reference_price`. `reference_price` is required on `reorg_transfer` rows and
must be null on every other type.

## Migration

None required. Stored rows read the new column as null. To add the events
the lake is missing:

```bash
cne repair corporate-action-gaps            # plan, offline
cne repair corporate-action-gaps --apply    # fetch Baostock and CNINFO, publish one revision
```

Readers that recompute factors from actions must use `previous close ÷
reference_price` on a `reorg_transfer` day instead of the share formula.

## Rollback

The repair publishes a new revision and retains the previous one;
`load("corporate_actions", revision=<previous>)` reads the lake without
these rows. Older code rejects nothing: it ignores the unknown column, but
would price a `reorg_transfer` day with the share formula.
