from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from cnequity.adapters.cninfo.reviewed_holder_notices import save_holder_evidence
from cnequity.quality import cash_rights


@pytest.fixture(autouse=True)
def _synthetic_original_record_date(monkeypatch):
    monkeypatch.setattr(cash_rights, "_legacy_record_date", lambda *a: date(2020, 6, 18))


def _entry() -> dict:
    return {
        "symbol": "600025.SH",
        "ex_date": "2020-06-19",
        "record_date": "2020-06-18",
        "payment_date": "2020-06-19",
        "holder_classes": {"tradable_a": "0.18", "founder": "0.14913"},
        "source_document_id": "notice-1",
        "source_sha256": "a" * 64,
        "source_published_at": "2020-06-11T16:00:00+00:00",
        "evidence_status": "verified_repaired",
    }


def _action(*, cash: float = 0.18, payment: date = date(2020, 6, 19)) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["600025.SH"],
            "ex_date": [date(2020, 6, 19)],
            "action_type": ["cash_dividend"],
            "cash_dividend": [cash],
            "payment_date": [payment],
            "payment_source": [
                "issuer_notice:notice-1:sha256:" + "a" * 64 + ":holder_class_cash_verified"
            ],
        }
    )


def test_reviewed_rights_are_revision_bound_and_immutable(tmp_path, monkeypatch):
    save_holder_evidence(tmp_path / "meta", "run", "600025.SH", [_entry()])
    monkeypatch.setattr(cash_rights, "committed_revision", lambda *a, **kw: (7, "revision-7"))
    monkeypatch.setattr(cash_rights, "load", lambda *a, **kw: _action())
    monkeypatch.setattr(cash_rights, "_code_sha256", lambda: "code-hash")
    payload = cash_rights.reviewed_rights_inventory(
        data_root=tmp_path, start=date(2016, 1, 1), end=date(2024, 12, 31)
    )
    assert payload["event_count"] == 1
    assert payload["count"] == 2
    assert {row["record_date"] for row in payload["rights"]} == {"2020-06-18"}
    assert payload["record_date_status"] == "verified_from_original_notice"
    assert payload["corporate_actions_revision_id"] == "revision-7"
    target = cash_rights.save_reviewed_rights(payload, tmp_path / "out")
    assert target == cash_rights.save_reviewed_rights(payload, tmp_path / "out")
    assert target.name.startswith("cash-rights-")


@pytest.mark.parametrize(
    ("cash", "payment", "message"),
    [
        (0.15, date(2020, 6, 19), "tradable A cash"),
        (0.18, date(2020, 6, 20), "payment date"),
    ],
)
def test_reviewed_rights_reject_changed_lake_action(tmp_path, monkeypatch, cash, payment, message):
    save_holder_evidence(tmp_path / "meta", "run", "600025.SH", [_entry()])
    monkeypatch.setattr(cash_rights, "committed_revision", lambda *a, **kw: (7, "revision-7"))
    monkeypatch.setattr(cash_rights, "load", lambda *a, **kw: _action(cash=cash, payment=payment))
    with pytest.raises(ValueError, match=message):
        cash_rights.reviewed_rights_inventory(
            data_root=tmp_path, start=date(2016, 1, 1), end=date(2024, 12, 31)
        )


def test_protected_cash_rights_window_refused_before_lake_read(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("protected range attempted a lake read")

    monkeypatch.setattr(cash_rights, "committed_revision", forbidden)
    with pytest.raises(ValueError, match="research holdout"):
        cash_rights.reviewed_rights_inventory(
            data_root=tmp_path,
            start=date(2024, 1, 1),
            end=date(2025, 1, 1),
            holdout_start=date(2025, 1, 1),
        )


def test_reviewed_action_requires_its_source_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(cash_rights, "committed_revision", lambda *a, **kw: (7, "revision-7"))
    monkeypatch.setattr(cash_rights, "load", lambda *a, **kw: _action())
    with pytest.raises(ValueError, match="lacks holder evidence"):
        cash_rights.reviewed_rights_inventory(
            data_root=tmp_path, start=date(2016, 1, 1), end=date(2024, 12, 31)
        )


def test_reviewed_rights_reject_tampered_source_file(tmp_path, monkeypatch):
    path = save_holder_evidence(tmp_path / "meta", "run", "600025.SH", [_entry()])
    path.write_bytes(path.read_bytes() + b" ")
    monkeypatch.setattr(cash_rights, "committed_revision", lambda *a, **kw: (7, "revision-7"))
    monkeypatch.setattr(cash_rights, "load", lambda *a, **kw: _action())
    with pytest.raises(ValueError, match="content hash mismatch"):
        cash_rights.reviewed_rights_inventory(
            data_root=tmp_path, start=date(2016, 1, 1), end=date(2024, 12, 31)
        )


def test_reviewed_rights_use_explicit_pinned_action_revision(tmp_path, monkeypatch):
    save_holder_evidence(tmp_path / "meta", "run", "600025.SH", [_entry()])
    monkeypatch.setattr(cash_rights, "committed_revision", lambda *a, **kw: (8, "latest"))
    seen = []

    def load_pinned(*args, **kwargs):
        seen.append(kwargs["revision"])
        return _action()

    monkeypatch.setattr(cash_rights, "load", load_pinned)
    payload = cash_rights.reviewed_rights_inventory(
        data_root=tmp_path,
        start=date(2016, 1, 1),
        end=date(2024, 12, 31),
        revision_id="pinned-7",
    )
    assert seen == ["pinned-7"]
    assert payload["corporate_actions_revision_id"] == "pinned-7"
    assert payload["corporate_actions_revision"] is None


def test_reviewed_rights_reject_sidecar_record_date_disagreeing_with_pdf(tmp_path, monkeypatch):
    entry = {**_entry(), "record_date": "2020-06-17"}
    save_holder_evidence(tmp_path / "meta", "run", "600025.SH", [entry])
    monkeypatch.setattr(cash_rights, "committed_revision", lambda *a, **kw: (7, "revision-7"))
    monkeypatch.setattr(cash_rights, "load", lambda *a, **kw: _action())
    with pytest.raises(ValueError, match="disagrees with original PDF"):
        cash_rights.reviewed_rights_inventory(
            data_root=tmp_path, start=date(2016, 1, 1), end=date(2024, 12, 31)
        )
