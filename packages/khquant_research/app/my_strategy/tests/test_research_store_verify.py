"""ResearchStore.verify: integrity re-verification without decode (round 11)."""
import gzip
import json

import pytest

from my_strategy.storage.research_store import ResearchStore, canonical


@pytest.fixture
def store(tmp_path):
    instance = ResearchStore(root=tmp_path / "store")
    yield instance
    instance.close()


def _entry(store, payload=None):
    identity = {"kind": "probe", "n": 1}
    result = payload if payload is not None else {"rows": list(range(1000)), "text": "x" * 5000}
    store.put(identity, result, namespace="analysis_previews")
    return identity, result


def test_verify_true_on_live_entry(store):
    identity, _ = _entry(store)
    assert store.verify(identity, namespace="analysis_previews") is True


def test_verify_false_on_plain_miss(store):
    assert store.verify({"kind": "absent"}, namespace="analysis_previews") is False


def _mutable(path):
    """Stored objects are read-only (tamper protection); GC re-adds 0o200 first."""
    path.chmod(path.stat().st_mode | 0o200)


def test_verify_does_not_decode(store, monkeypatch):
    identity, _ = _entry(store)
    monkeypatch.setattr(gzip, "decompress", lambda *a, **k: (_ for _ in ()).throw(AssertionError("decompressed")))
    assert store.verify(identity, namespace="analysis_previews") is True


def test_verify_raises_on_tampered_bytes(store):
    identity, _ = _entry(store)
    key = store.cache_key(identity, "analysis_previews")
    row = store.connection.execute("SELECT sha256 FROM results WHERE cache_key=?", (key,)).fetchone()
    path = store._path(row["sha256"])
    payload = bytearray(path.read_bytes())
    payload[-10] ^= 0xFF  # flip a byte inside the gzip stream
    _mutable(path)
    path.write_bytes(bytes(payload))
    with pytest.raises(ValueError, match="SHA/size mismatch"):
        store.verify(identity, namespace="analysis_previews")


def test_verify_false_after_gc_eviction(store):
    identity, _ = _entry(store)
    key = store.cache_key(identity, "analysis_previews")
    row = store.connection.execute("SELECT sha256 FROM results WHERE cache_key=?", (key,)).fetchone()
    path = store._path(row["sha256"])
    _mutable(path)
    path.unlink()
    store.connection.execute("DELETE FROM results WHERE cache_key=?", (key,))
    store.connection.execute("DELETE FROM objects WHERE sha256=?", (row["sha256"],))
    assert store.verify(identity, namespace="analysis_previews") is False


def test_verify_matches_get_bytes_semantics(store):
    """verify passes exactly when get would return the stored payload."""
    identity, result = _entry(store)
    assert store.get(identity, namespace="analysis_previews") == result
    assert store.verify(identity, namespace="analysis_previews") is True
    assert store.verify({"kind": "absent"}) is False
    assert store.get({"kind": "absent"}) is None


def test_verify_touches_access_order_like_get(store):
    identity, _ = _entry(store, payload={"small": True})
    key = store.cache_key(identity, "analysis_previews")
    store.connection.execute("UPDATE results SET accessed_at=? WHERE cache_key=?", (store.clock() - 120, key))
    store.connection.commit()
    assert store.verify(identity, namespace="analysis_previews") is True
    touched = store.connection.execute("SELECT accessed_at FROM results WHERE cache_key=?", (key,)).fetchone()[0]
    assert touched >= store.clock() - 5


def test_verify_rejects_drifted_identity(store):
    identity, _ = _entry(store)
    key = store.cache_key(identity, "analysis_previews")
    store.connection.execute("UPDATE results SET identity_json=? WHERE cache_key=?",
                             (canonical({"kind": "drifted"}), key))
    store.connection.commit()
    with pytest.raises(ValueError, match="identity drift"):
        store.verify(identity, namespace="analysis_previews")
