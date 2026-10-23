"""Hot in-process pack memo: bounded, root-scoped, file-existence-checked.

Entries are content-addressed and immutable, so repeated reads of the same
key may serve the shared object instead of re-deserializing — but only for
verified read-only families, only while the backing file still exists, and
never across store roots.
"""
import pytest

from my_strategy.core.run_context import stable_hash
from my_strategy.services import native_pack_cache as npc


@pytest.fixture(autouse=True)
def _clear_hot():
    npc._HOT_PACKS.clear()
    yield
    npc._HOT_PACKS.clear()


def _identity(kind, marker):
    return {"kind": kind, "symbol": "000001.SZ", "marker": marker}


def test_native_pack_memo_serves_same_object(tmp_path, monkeypatch):
    monkeypatch.setenv("KHQUANT_NATIVE_PACK_HOT_ENTRIES", "4")
    identity = _identity(npc.FORMAT, "a")
    pack = {"frame": [1, 2, 3]}
    npc.put_pack(identity, pack, root=tmp_path, quota_bytes=10**9)
    assert npc.get_pack(identity, root=tmp_path) is pack


def test_chart_family_is_memoized(tmp_path, monkeypatch):
    monkeypatch.setenv("KHQUANT_NATIVE_PACK_HOT_ENTRIES", "4")
    identity = _identity(npc.CHART_FORMAT, "c")
    pack = {"日线": {"bars": []}}
    npc.put_pack(identity, pack, root=tmp_path, quota_bytes=10**9)
    assert npc.get_pack(identity, root=tmp_path) is pack


def test_features_family_is_never_memoized(tmp_path, monkeypatch):
    monkeypatch.setenv("KHQUANT_NATIVE_PACK_HOT_ENTRIES", "4")
    identity = _identity(npc.FEATURES_FORMAT, "f")
    pack = {"frame": [1]}
    npc.put_pack(identity, pack, root=tmp_path, quota_bytes=10**9)
    first = npc.get_pack(identity, root=tmp_path)
    second = npc.get_pack(identity, root=tmp_path)
    assert first == pack and second == pack
    assert first is not pack and second is not first


def test_deleted_file_drops_memo_entry_and_misses(tmp_path, monkeypatch):
    monkeypatch.setenv("KHQUANT_NATIVE_PACK_HOT_ENTRIES", "4")
    identity = _identity(npc.FORMAT, "del")
    npc.put_pack(identity, {"v": 1}, root=tmp_path, quota_bytes=10**9)
    assert npc.get_pack(identity, root=tmp_path) is not None
    npc._pack_path(tmp_path, stable_hash(identity)).unlink()
    assert npc.get_pack(identity, root=tmp_path) is None
    assert (str(tmp_path), stable_hash(identity)) not in npc._HOT_PACKS


def test_capacity_eviction_forces_fresh_deserialization(tmp_path, monkeypatch):
    monkeypatch.setenv("KHQUANT_NATIVE_PACK_HOT_ENTRIES", "1")
    id_a, id_b = _identity(npc.FORMAT, "a"), _identity(npc.FORMAT, "b")
    pack_a = {"v": "a"}
    npc.put_pack(id_a, pack_a, root=tmp_path, quota_bytes=10**9)
    npc.put_pack(id_b, {"v": "b"}, root=tmp_path, quota_bytes=10**9)
    reread = npc.get_pack(id_a, root=tmp_path)
    assert reread == pack_a and reread is not pack_a
    assert npc.get_pack(id_a, root=tmp_path) is reread


def test_zero_capacity_disables_the_layer(tmp_path, monkeypatch):
    monkeypatch.setenv("KHQUANT_NATIVE_PACK_HOT_ENTRIES", "0")
    identity = _identity(npc.FORMAT, "z")
    pack = {"v": 1}
    npc.put_pack(identity, pack, root=tmp_path, quota_bytes=10**9)
    assert npc.get_pack(identity, root=tmp_path) is not pack
    assert not npc._HOT_PACKS


def test_memo_tokens_never_cross_roots(tmp_path, monkeypatch):
    monkeypatch.setenv("KHQUANT_NATIVE_PACK_HOT_ENTRIES", "4")
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    identity = _identity(npc.FORMAT, "iso")
    npc.put_pack(identity, {"v": 1}, root=root_a, quota_bytes=10**9)
    assert npc.get_pack(identity, root=root_b) is None


def test_read_pack_by_key_uses_the_memo(tmp_path, monkeypatch):
    monkeypatch.setenv("KHQUANT_NATIVE_PACK_HOT_ENTRIES", "4")
    identity = _identity(npc.FORMAT, "k")
    pack = {"v": 7}
    npc.put_pack(identity, pack, root=tmp_path, quota_bytes=10**9)
    key = stable_hash(identity)
    found = npc._read_pack_by_key(tmp_path, key)
    assert found == (identity, pack)
    again = npc._read_pack_by_key(tmp_path, key)
    assert again[1] is found[1]
    npc._pack_path(tmp_path, key).unlink()
    assert npc._read_pack_by_key(tmp_path, key) is None
