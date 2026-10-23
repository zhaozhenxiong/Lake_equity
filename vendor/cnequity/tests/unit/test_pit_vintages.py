"""PIT vintages: a restatement must add a row, never overwrite the original."""

import importlib.util
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from cnequity.config import Config
from cnequity.domain.schemas import PRIMARY_KEYS
from cnequity.query import load, load_with_receipt
from cnequity.query.reader import ReaderError
from cnequity.storage.parquet import StagingWriter, compact_dataset

_DATASET = "financial_statement_items"

# One fact — 000001.SZ 2024Q1 revenue — reported once, then restated downward.
_ORIGINAL = date(2024, 4, 20)
_RESTATED = date(2025, 3, 15)


def _row(announce: date, value: float, fetched: str) -> dict:
    return {
        "symbol": "000001.SZ",
        "report_period": "2024Q1",
        "statement_type": "income",
        "item_code": "revenue",
        "item_value": value,
        "announce_date": announce,
        "source": "eastmoney",
        "data_version": "v1",
        "fetched_at": fetched,
    }


def _frame(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows).with_columns(
        pl.col("announce_date").cast(pl.Date),
        pl.col("fetched_at").str.to_datetime(time_unit="us", time_zone="UTC"),
    )


def _write_curated(cfg: Config, rows: list[dict]) -> None:
    part = cfg.curated_root / _DATASET / "report_period=2024Q1"
    part.mkdir(parents=True, exist_ok=True)
    _frame(rows).write_parquet(part / "part-merged.parquet")


def test_announce_date_is_part_of_the_primary_key():
    assert "announce_date" in PRIMARY_KEYS[_DATASET]


def test_compact_keeps_both_vintages(tmp_path):
    """Without announce_date in the PK the restatement would erase the original."""
    cfg = Config(data_root=tmp_path / "data")
    StagingWriter(cfg.staging_root).write_batch(
        _DATASET,
        "run-1",
        "batch-0",
        _frame(
            [
                _row(_ORIGINAL, 100.0, "2024-04-20T09:00:00+00:00"),
                _row(_RESTATED, 90.0, "2025-03-15T09:00:00+00:00"),
            ]
        ),
    )

    compact_dataset(
        cfg.staging_root, cfg.curated_root, _DATASET, "run-1", partition_col="report_period"
    )

    out = pl.read_parquet(
        cfg.curated_root / _DATASET / "report_period=2024Q1" / "part-merged.parquet"
    )
    assert sorted(out["announce_date"].to_list()) == [_ORIGINAL, _RESTATED]


def test_as_of_before_restatement_returns_the_original_value(tmp_path):
    cfg = Config(data_root=tmp_path / "data")
    _write_curated(
        cfg,
        [
            _row(_ORIGINAL, 100.0, "2024-04-20T09:00:00+00:00"),
            _row(_RESTATED, 90.0, "2025-03-15T09:00:00+00:00"),
        ],
    )

    df = load(_DATASET, as_of="2024-06-30", config=cfg)

    assert df.height == 1
    assert df["item_value"][0] == 100.0


def test_as_of_after_restatement_returns_the_revised_value_once(tmp_path):
    """Both vintages qualify on date; only the one current then may be returned."""
    cfg = Config(data_root=tmp_path / "data")
    _write_curated(
        cfg,
        [
            _row(_ORIGINAL, 100.0, "2024-04-20T09:00:00+00:00"),
            _row(_RESTATED, 90.0, "2025-03-15T09:00:00+00:00"),
        ],
    )

    df = load(_DATASET, as_of="2025-06-30", config=cfg)

    assert df.height == 1, "a restated fact must not be double-counted"
    assert df["item_value"][0] == 90.0


@pytest.mark.parametrize("mode", [None, "best_effort"])
def test_default_includes_backfilled_history_and_records_its_quality(tmp_path, mode):
    """A later collection must not empty an exploratory historical read."""
    cfg = Config(data_root=tmp_path / "data")
    row = _row(_ORIGINAL, 90.0, "2026-08-22T09:00:00+00:00")
    row["source"] = "eastmoney_backfill"
    _write_curated(cfg, [row])

    options = {} if mode is None else {"pit_mode": mode}
    result = load_with_receipt(_DATASET, as_of="2025-06-30", config=cfg, **options)
    explicit_none = load(_DATASET, as_of="2025-06-30", config=cfg, pit_mode=None)

    assert result.frame["item_value"].to_list() == [90.0]
    assert result.frame["pit_is_exact"].to_list() == [False]
    assert result.frame["pit_quality"].to_list() == ["reconstructed"]
    assert result.frame.equals(explicit_none)
    assert result.receipt["pit"]["mode"] == "best_effort"
    assert result.receipt["pit"]["exact_counts"] == {"False": 1}


def test_explicit_strict_rejects_reconstructed_and_best_effort_marks_it(tmp_path):
    cfg = Config(data_root=tmp_path / "data")
    row = _row(_ORIGINAL, 120.0, "2026-08-22T09:00:00+00:00")
    row["source"] = "eastmoney_backfill"
    _write_curated(cfg, [row])

    strict = load(
        _DATASET,
        as_of="2025-06-30",
        pit_mode="strict",
        config=cfg,
    )
    assert strict.is_empty()

    best_effort = load(
        _DATASET,
        as_of="2025-06-30",
        pit_mode="best_effort",
        config=cfg,
    )
    assert best_effort["item_value"].to_list() == [120.0]
    assert best_effort["pit_is_exact"].to_list() == [False]
    assert best_effort["pit_quality"].to_list() == ["reconstructed"]


@pytest.mark.parametrize("mode", [None, "best_effort", "strict"])
def test_known_source_times_are_bounds_in_all_modes(tmp_path, mode):
    cfg = Config(data_root=tmp_path / "data")
    row = _row(_ORIGINAL, 100.0, "2024-04-20T09:00:00+00:00")
    row.update(
        {
            "available_at": "2024-04-21T09:00:00+00:00",
            "source_published_at": "2024-04-21T09:00:00+00:00",
            "observed_at": "2024-04-21T09:00:00+00:00",
        }
    )
    _write_curated(cfg, [row])

    before_available = load(
        _DATASET,
        as_of="2024-04-20",
        pit_mode=mode,
        config=cfg,
    )
    after_available = load(
        _DATASET,
        as_of="2024-04-21",
        pit_mode="strict",
        config=cfg,
    )
    assert before_available.is_empty()
    assert after_available["pit_is_exact"].to_list() == [True]


def test_all_vintages_exposes_the_revision_history(tmp_path):
    cfg = Config(data_root=tmp_path / "data")
    _write_curated(
        cfg,
        [
            _row(_ORIGINAL, 100.0, "2024-04-20T09:00:00+00:00"),
            _row(_RESTATED, 90.0, "2025-03-15T09:00:00+00:00"),
        ],
    )

    df = load(_DATASET, as_of="2025-06-30", all_vintages=True, config=cfg)

    assert sorted(df["item_value"].to_list()) == [90.0, 100.0]


def test_vintages_are_collapsed_per_item_not_globally(tmp_path):
    """Two different items must both survive; only same-key vintages collapse."""
    cfg = Config(data_root=tmp_path / "data")
    net_profit = {**_row(_ORIGINAL, 12.0, "2024-04-20T09:00:00+00:00"), "item_code": "net_profit"}
    _write_curated(
        cfg,
        [
            _row(_ORIGINAL, 100.0, "2024-04-20T09:00:00+00:00"),
            _row(_RESTATED, 90.0, "2025-03-15T09:00:00+00:00"),
            net_profit,
        ],
    )

    df = load(_DATASET, as_of="2025-06-30", config=cfg)

    assert sorted(df["item_code"].to_list()) == ["net_profit", "revenue"]


def test_as_of_is_still_required(tmp_path):
    cfg = Config(data_root=tmp_path / "data")
    _write_curated(cfg, [_row(_ORIGINAL, 100.0, "2024-04-20T09:00:00+00:00")])

    with pytest.raises(ReaderError, match="requires as_of"):
        load(_DATASET, config=cfg)


def test_validate_dataframe_carries_pit_columns_only_for_pit_datasets():
    """The optional bitemporal columns must survive schema projection.

    `validate_dataframe` selects exactly the registered columns, which used to
    drop these four on the way to disk — so a PIT dataset could never store
    one, and every reader re-derived them instead.
    """
    from cnequity.domain.pit import PIT_STORAGE_COLUMNS, normalize_pit_storage_columns
    from cnequity.domain.schemas import validate_dataframe

    frame = normalize_pit_storage_columns(
        _frame([_row(_ORIGINAL, 100.0, "2024-04-20T09:00:00+00:00")]), _DATASET
    )
    kept = validate_dataframe(frame, _DATASET)
    assert set(PIT_STORAGE_COLUMNS).issubset(kept.columns)
    assert kept["revision_id"].null_count() == 0
    assert kept.schema["observed_at"] == pl.Datetime("us", "UTC")

    # A frame that never had them must not gain them here: this function
    # carries columns through, it does not manufacture them.
    bare = validate_dataframe(
        _frame([_row(_ORIGINAL, 100.0, "2024-04-20T09:00:00+00:00")]), _DATASET
    )
    assert not set(PIT_STORAGE_COLUMNS) & set(bare.columns)


def test_compact_persists_pit_columns_without_minting_a_revision(tmp_path):
    """Migration writes the columns; it is not itself a business change."""
    from cnequity.domain.pit import PIT_STORAGE_COLUMNS

    cfg = Config(data_root=tmp_path / "data")
    _write_curated(cfg, [_row(_ORIGINAL, 100.0, "2024-04-20T09:00:00+00:00")])
    StagingWriter(cfg.staging_root).write_batch(
        _DATASET, "run-1", "batch-0", _frame([_row(_ORIGINAL, 100.0, "2024-04-20T09:00:00+00:00")])
    )
    changed: list = []
    compact_dataset(
        cfg.staging_root,
        cfg.curated_root,
        _DATASET,
        "run-1",
        partition_col="report_period",
        changed_files=changed,
    )
    out = cfg.curated_root / _DATASET / "report_period=2024Q1" / "part-merged.parquet"
    stored = pl.read_parquet(out)
    assert set(PIT_STORAGE_COLUMNS).issubset(stored.columns)
    assert stored["revision_id"].null_count() == 0
    assert changed == []


def test_refetch_with_new_fetched_at_stays_a_physical_no_op(tmp_path):
    """`observed_at` aliases `fetched_at` and must not count as evidence.

    Without this the bitemporal columns would re-mint a revision on every
    reconciliation pass, and each revision copies the whole dataset into a new
    generation.
    """
    cfg = Config(data_root=tmp_path / "data")
    _write_curated(cfg, [_row(_ORIGINAL, 100.0, "2024-04-20T09:00:00+00:00")])
    out = cfg.curated_root / _DATASET / "report_period=2024Q1" / "part-merged.parquet"

    StagingWriter(cfg.staging_root).write_batch(
        _DATASET, "run-1", "batch-0", _frame([_row(_ORIGINAL, 100.0, "2024-04-20T09:00:00+00:00")])
    )
    compact_dataset(
        cfg.staging_root,
        cfg.curated_root,
        _DATASET,
        "run-1",
        partition_col="report_period",
        changed_files=[],
    )
    inode = out.stat().st_ino

    # Same fact, observed later.
    StagingWriter(cfg.staging_root).write_batch(
        _DATASET, "run-2", "batch-0", _frame([_row(_ORIGINAL, 100.0, "2026-01-02T09:00:00+00:00")])
    )
    changed: list = []
    compact_dataset(
        cfg.staging_root,
        cfg.curated_root,
        _DATASET,
        "run-2",
        partition_col="report_period",
        changed_files=changed,
    )
    assert changed == []
    assert out.stat().st_ino == inode

    # A real restatement must still be detected.
    StagingWriter(cfg.staging_root).write_batch(
        _DATASET, "run-3", "batch-0", _frame([_row(_RESTATED, 80.0, "2026-01-03T09:00:00+00:00")])
    )
    real: list = []
    compact_dataset(
        cfg.staging_root,
        cfg.curated_root,
        _DATASET,
        "run-3",
        partition_col="report_period",
        changed_files=real,
    )
    assert real, "a restatement must mint a revision"


def test_revision_id_width_is_a_storage_decision():
    """Pinned: the digest is stored on every row, so its width is not free.

    96 bits keeps the collision probability negligible (~1e-15 across the
    current PIT row count, ~1e-13 at a hundred times that) while costing 73 MB
    instead of 389 MB — on datasets totalling 190 MB, each copy of which is
    duplicated whole into every committed generation.
    """
    from cnequity.domain.pit import REVISION_ID_HEX_CHARS, revision_id_for_row

    assert REVISION_ID_HEX_CHARS == 24
    digest = revision_id_for_row(_row(_ORIGINAL, 100.0, "2024-04-20T09:00:00+00:00"))
    assert len(digest) == 24
    assert all(char in "0123456789abcdef" for char in digest)


def test_revision_id_ignores_observation_time_but_tracks_value():
    """Re-observing a fact keeps its identity; restating it changes identity."""
    from cnequity.domain.pit import revision_id_for_row

    base = _row(_ORIGINAL, 100.0, "2024-04-20T09:00:00+00:00")
    later = _row(_ORIGINAL, 100.0, "2026-01-02T09:00:00+00:00")
    restated = _row(_ORIGINAL, 80.0, "2024-04-20T09:00:00+00:00")

    assert revision_id_for_row(base) == revision_id_for_row(later)
    assert revision_id_for_row(base) != revision_id_for_row(restated)


def test_cninfo_archive_recovery_requires_full_announcement_identity():
    script = Path(__file__).parents[2] / "scripts" / "migrations" / "migrate_pit_vintages.py"
    spec = importlib.util.spec_from_file_location("migrate_pit_vintages", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    existing = pl.DataFrame(
        {
            "announcement_id": ["a", "b"],
            "symbol": ["002358.SZ", "002358.SZ"],
            "announce_date": [date(2019, 6, 28), date(2019, 6, 28)],
            "title": ["实施公告", "另一公告"],
            "url": ["a.pdf", "b.pdf"],
            "source_published_at": [None, None],
            "available_at": [None, None],
        },
        schema_overrides={
            "source_published_at": pl.Datetime("us", "UTC"),
            "available_at": pl.Datetime("us", "UTC"),
        },
    )
    replayed = pl.DataFrame(
        {
            "announcement_id": ["a", "b"],
            "symbol": ["002358.SZ", "002358.SZ"],
            "announce_date": [date(2019, 6, 28), date(2019, 6, 28)],
            "title": ["实施公告", "标题已变化"],
            "url": ["a.pdf", "b.pdf"],
            "source_published_at": [
                "2019-06-27T16:00:00+00:00",
                "2019-06-27T16:00:00+00:00",
            ],
            "available_at": [
                "2019-06-28T16:00:00+00:00",
                "2019-06-28T16:00:00+00:00",
            ],
        }
    ).with_columns(
        pl.col("source_published_at", "available_at").str.to_datetime(
            time_unit="us", time_zone="UTC"
        )
    )

    recovered = module._recovered_announcement_rows(existing, replayed)

    assert recovered["announcement_id"].to_list() == ["a"]
    assert recovered["source_published_at"].null_count() == 0


def test_same_announce_date_correction_preserves_original_and_first_observation(tmp_path):
    cfg = Config(data_root=tmp_path)
    batches = [
        [_row(_ORIGINAL, 100.0, "2024-04-20T09:00:00Z")],
        [_row(_ORIGINAL, 90.0, "2024-05-20T09:00:00Z")],
        [
            _row(_ORIGINAL, 100.0, "2024-06-20T09:00:00Z"),
            {**_row(_ORIGINAL, 3.0, "2024-06-20T09:00:00Z"), "item_code": "net_profit"},
        ],
    ]
    for index, rows in enumerate(batches):
        run = f"run-{index}"
        StagingWriter(cfg.staging_root).write_batch(_DATASET, run, "batch", _frame(rows))
        compact_dataset(
            cfg.staging_root, cfg.curated_root, _DATASET, run, partition_col="report_period"
        )
    early = load(_DATASET, config=cfg, as_of="2024-04-30", items=["revenue"], pit_mode="strict")
    assert early["item_value"].to_list() == [100.0]
    later = load(_DATASET, config=cfg, as_of="2024-07-01", items=["revenue"])
    assert later["item_value"].to_list() == [90.0]
    all_rows = load(_DATASET, config=cfg, as_of="2024-07-01", items=["revenue"], all_vintages=True)
    assert sorted(all_rows["item_value"].to_list()) == [90.0, 100.0]
    from cnequity.quality.dataset_checks import _partitioned_pk_duplicate_count

    root = cfg.curated_root / _DATASET
    assert (
        _partitioned_pk_duplicate_count(
            list(root.rglob("*.parquet")), _DATASET, "report_period", root
        )
        == 0
    )
