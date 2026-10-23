# Data contract

`DatasetSpec`, `DATASET_SCHEMAS` and `PRIMARY_KEYS` are CNEquity's single source
of truth. They can be exported as stable JSON for revisions, downstream jobs and CI to store and compare:

```bash
cne contract show daily_bars
cne contract show --out meta/dataset-contract.json
cne contract validate meta/dataset-contract.json
cne contract diff meta/old-contract.json meta/dataset-contract.json
```

Since 0.8.0, the full contract of every release is stored in the `contracts/`
directory at the repository root (for example `contracts/v0.8.0.json`) as the stable baseline for reviewing the next release.
0.7.3 and earlier did not export a machine-readable contract, so 0.8.0 is the first baseline usable for cross-version
`contract diff`.

Omitting the dataset argument to `show` prints the full contract for all registered datasets. The output of `show --out` and of Python `export_contract()` includes
a top-level `fingerprint` (SHA-256); the same registry exported in different processes yields the same
fingerprint. With no argument, `contract validate` validates the current registry; given a file, it validates a portable contract,
and only with `--against-registry` does it require the file to match the current registry exactly.

Each dataset record contains:

- `schema_version`: the version of the column shape. Adding backward-compatible columns does not require bumping it; removing columns or changing
  types must bump it and be called out in the release notes.
- `contract_level` and `compatibility`: currently registered datasets default to `stable` / `additive`.
- `pit_grade` is a 0.x compatibility alias (`none` / `strict` / `partial`); the new
  `pit_quality` uses `strict` / `reconstructed` / `snapshot_only`. Currently
  `financial_statement_items` and the historical shareholder backfills are `reconstructed`/`partial`;
  only `announcement_index` is strict PIT. `availability_col` defaults to
  `announce_date`.

  !!! warning "Check `pit` first, then `pit_quality`"

      **`pit_quality` is a claim only when `pit: true`.** Non-PIT by-date data
      has no PIT quality to speak of, but the exported contract must be complete (every field of every dataset has a
      value). Such tables use `not_applicable`,
      **including `daily_bars`**, and the only dataset that actually claims strict PIT is
      `announcement_index`.

      For current counts and distribution, read `cne contract show`; do not treat historical statistics as a fixed contract for a new release.

      The deciding fields are still `pit` (boolean) and `pit_grade`, not `pit_quality` on its own;
      an empty `pit_storage_columns` likewise means the table has no bitemporal columns.

      ```python
      spec = contract["datasets"]["daily_bars"]
      spec["pit"]           # False  ← this is the answer
      spec["pit_grade"]     # "none"
      spec["pit_quality"]   # "not_applicable"
      ```

      Earlier versions let non-PIT tables fall back to the literal `strict`, so reading that field alone turned "nobody has
      considered PIT for this table" into "this table's PIT is exact". `not_applicable` was introduced to
      remove that ambiguity.
- `pit_modes` is fixed to `strict` / `best_effort`. strict allows only vintages known as of the
  cutoff date; best-effort may keep backfilled current values but returns `pit_is_exact=False`.
- `pit_storage_columns` are the optional bitemporal columns: `available_at`,
  `source_published_at`, `observed_at`, `revision_id`. Old files missing these columns have them filled on read,
  so they do not become unreadable; `scripts/migrations/migrate_pit_vintages.py` can add them idempotently with dry-run/apply.
- `unit_contract`: the canonical units for prices, share counts, amounts, ratios and other numeric values; tables without a special numeric
  convention use `canonical`.
- `schema` / `columns`, `primary_key` / `primary_keys`: compatibility aliases for the same fact.

`diff` marks new columns and new datasets as compatible; dropped columns, changed types, changed primary keys, unit changes,
PIT/availability changes, and changes to history-fetch semantics (including `snapshot`, backfill source and source history floor)
are all marked breaking. When breaking changes are found, the command exits with code 1 by default; to report them but allow
continuing, use `--allow-breaking`.

Python usage:

```python
from cnequity.domain.contracts import (
    contract_fingerprint,
    dataset_contract,
    diff_contracts,
    export_contract,
    validate_contract,
)

row = dataset_contract("daily_bars")
fingerprint = contract_fingerprint("daily_bars")
document = export_contract()  # dict; export_contract(path) also writes JSON
assert validate_contract(document) == []
changes = diff_contracts(document, document)
assert not changes["is_breaking"]
```

This metadata describes only the read/evolution contract and does not rewrite existing Parquet rows. `data_version`
is still dedicated to recording reinterpretations of numeric semantics (for example, the unit of daily volume), separately from
`schema_version`.
