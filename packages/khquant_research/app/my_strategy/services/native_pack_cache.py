"""Shared content-addressed compute-object store for analysis and scan paths.

Three object families share one bounded on-disk store:

- native_pack_v1: build_native_evidence packs, a deterministic function of
  the observed feature frame, the verified calendar, the native contract
  hashes and the compute code. Single-stock analysis and scans share them
  instead of re-running the per-day structure/ICT/Wyckoff advance per request.
- chart_payload_v1: unfiltered CzscSession chart payloads, a deterministic
  function of the raw frame cut to the decision date, the strategy settings
  and the display code. Callers apply filter_chart_payload per request, so
  one entry serves every chart start date.
- features_frame_v1: prepared feature frames, a deterministic function of
  the liquidity-augmented raw frame, the verified calendar, the feature
  profile and the feature code. Scan workers and analysis share them through
  prepare_observed_data.

Identity is content-based: the input frame hash, calendar/settings hashes,
and the sha256 of every compute-bearing source file. Any data, calendar,
config or code change misses the cache and rebuilds; nothing stale can ever
be served.

Native packs are built with capture_state=True and indexed per symbol by
their static identity (everything except the frame hash). A miss on an
extended frame resumes the newest strict-prefix pack through
build_native_evidence's explicit incremental state, advancing only appended
bars and stitching the delta onto the stored prefix pack. The resume is
re-validated by the evidence builder (schema/config/raw-prefix/calendar
SHAs); any doubt falls back to full replay. Chart payloads use the same
continuation index through dump_chart_session/restore_chart_session: the
prefix frame hash is verified before restore, and payload(None) is then
recomputed wholesale from the resumed session, so entries stay complete
without stitching.

Writes go through a temporary file plus atomic replacement, so readers in
scan worker processes never observe a partial pack. The store is bounded by a
byte quota: puts trigger a time-limited sweep that evicts the oldest packs
first (LRU by modification time) and sweeps orphaned temporary files.
"""
from __future__ import annotations

from collections import OrderedDict
import gzip
import hashlib
import json
import os
import pickle
import time
from pathlib import Path

import pandas as pd

from my_strategy.core.paths import PROCESSED_DATA_ROOT
from my_strategy.core.run_context import stable_hash

FORMAT = "native_pack_v1"
CHART_FORMAT = "chart_payload_v1"
FEATURES_FORMAT = "features_frame_v1"
DEFAULT_QUOTA_BYTES = 8 * 1024 ** 3
_GC_INTERVAL_SECONDS = 3600.0
_TMP_MAX_AGE_SECONDS = 3600.0
# Compute-bearing modules for build_native_evidence(config=None). Contract
# hashes in the identity cover configs and schema constants; these hashes
# cover the code that interprets them. Names are relative to this directory;
# "../" entries reach sibling package modules.
_CODE_FILES = (
    "czsc_native_evidence.py",
    "czsc_analysis.py",
    "czsc_incremental_state.py",
    "czsc_market_features.py",
    "czsc_price_volume_features.py",
    "czsc_price_volume_events.py",
    "czsc_ict_events.py",
    "czsc_trend_support.py",
    "native_pack_cache.py",
)
# Chart payloads come from CzscSession.advance_structure + payload; the
# settings hash covers the vendored engine tree (source_tree_sha256) and the
# strategy config, so only the display/advance code is listed here.
_CHART_CODE_FILES = (
    "czsc_analysis.py",
    "czsc_chart_indicators.py",
    "czsc_incremental_state.py",
    "native_pack_cache.py",
    "../adapters/czsc_adapter.py",
    "../web_dashboard/trend_strategy.py",
)
# Feature frames come from build_features + profile/wyckoff preparation.
# build_features advances a CzscSession internally, so the analysis module and
# the adapter (engine contract, source mapping) are compute-bearing here too.
_FEATURES_CODE_FILES = (
    "czsc_market_features.py",
    "czsc_market_profiles.py",
    "czsc_price_volume_features.py",
    "czsc_price_volume_events.py",
    "czsc_trend_support.py",
    "czsc_analysis.py",
    "czsc_incremental_state.py",
    "native_pack_cache.py",
    "../adapters/czsc_adapter.py",
    "../web_dashboard/trend_strategy.py",
)

_last_gc_monotonic = 0.0

# In-process hot object layer. Store entries are content-addressed and
# immutable, so a small per-process LRU can serve repeated reads of the same
# entry without re-deserializing (the analysis server reads the same symbol's
# pack on nearly every request). Safety rails:
#   * only verified read-only families are memoized (native/chart callers copy
#     or rebuild what they touch; the features frame is mutated in place by
#     prepare_observed_data's raw-column merge and is never memoized);
#   * the memo key includes the store root, so isolated test stores never
#     share entries;
#   * a hit still stats the backing file and falls through to disk when the
#     file was deleted or evicted, preserving every delete-then-fallback
#     contract (continuation candidates, quota GC, corruption sweeps);
#   * KHQUANT_NATIVE_PACK_HOT_ENTRIES=0 disables the layer (scan workers set
#     this: each pack is read once per process and a resident memo would only
#     hold memory).
_HOT_KINDS = frozenset({FORMAT, CHART_FORMAT})
_HOT_PACKS: "OrderedDict[tuple[str, str], tuple[dict, object]]" = OrderedDict()


def _hot_capacity() -> int:
    try:
        return int(os.environ.get("KHQUANT_NATIVE_PACK_HOT_ENTRIES", "6"))
    except ValueError:
        return 6


def _hot_read(root: Path, key: str) -> tuple[dict, object] | None:
    """Memoized (identity, pack) for ``key``, or None when unusable."""
    token = (str(root), key)
    found = _HOT_PACKS.get(token)
    if found is None:
        return None
    if _hot_capacity() <= 0 or not _pack_path(root, key).is_file():
        _HOT_PACKS.pop(token, None)
        return None
    _HOT_PACKS.move_to_end(token)
    return found


def _hot_store(root: Path, key: str, kind: str, identity: dict, pack) -> None:
    capacity = _hot_capacity()
    if capacity <= 0 or kind not in _HOT_KINDS:
        return
    token = (str(root), key)
    _HOT_PACKS[token] = (identity, pack)
    _HOT_PACKS.move_to_end(token)
    while len(_HOT_PACKS) > capacity:
        _HOT_PACKS.popitem(last=False)


def default_root() -> Path:
    override = os.environ.get("KHQUANT_NATIVE_PACK_ROOT")
    return Path(override).expanduser() if override else PROCESSED_DATA_ROOT / "czsc" / "native_packs"


def default_quota_bytes() -> int:
    override = os.environ.get("KHQUANT_NATIVE_PACK_QUOTA_BYTES")
    if override:
        try:
            return max(0, int(override))
        except ValueError:
            pass
    return DEFAULT_QUOTA_BYTES


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _code_hashes(names: tuple[str, ...] = _CODE_FILES) -> dict[str, str]:
    base = Path(__file__).resolve().parent
    return {name: _file_sha256((base / name).resolve()) for name in names}


def canonical_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalization shared by analysis and scan builders.

    The analysis path stringifies dates before building while scan workers
    pass the feature frame as-is; both must produce the same identity and the
    same cached pack, so the cache always sees (and builds from) ISO dates.
    """
    out = frame.copy()
    out["date"] = pd.to_datetime(out.date).dt.strftime("%Y-%m-%d")
    return out


def frame_sha256(frame: pd.DataFrame) -> str:
    digest = hashlib.sha256()
    digest.update("\x1f".join(map(str, frame.columns)).encode("utf-8"))
    for column in frame.columns:
        series = frame[column]
        digest.update(b"\x1e")
        digest.update(str(series.dtype).encode("utf-8"))
        try:
            digest.update(pd.util.hash_pandas_object(series, index=False).to_numpy().tobytes())
        except TypeError:
            # Object columns with list/dict cells (e.g. reason_codes) cannot be
            # hashed row-wise; canonical JSON is deterministic for them.
            digest.update(json.dumps(series.tolist(), ensure_ascii=False,
                                     sort_keys=True).encode("utf-8"))
    return digest.hexdigest()


def native_pack_identity(frame: pd.DataFrame, calendar_dates) -> dict:
    """Full identity payload for the pack build_native_evidence would return."""
    from my_strategy.services.czsc_native_evidence import NATIVE_EVIDENCE_VERSION, native_contract
    contract = native_contract()
    return {
        "kind": FORMAT,
        "symbol": str(frame.iloc[0].symbol),
        "frame_sha256": frame_sha256(frame),
        "calendar_sha256": stable_hash(list(calendar_dates)),
        "native_version": NATIVE_EVIDENCE_VERSION,
        "schema_hash": contract["schema_hash"],
        "config_hash": contract["config_hash"],
        "code": _code_hashes(),
    }


def _pack_path(root: Path, key: str) -> Path:
    return root / key[:2] / f"{key}.pkl.gz"


def get_pack(identity: dict, *, root: Path | None = None) -> dict | None:
    """Return the pack for ``identity``, or None on miss/corruption.

    Features-family reads are fresh deserializations, so their callers receive
    independent mutable objects; the stored bytes are never aliased. Native and
    chart families may be served from the in-process hot layer as shared
    immutable objects — callers must not mutate them (all current native/chart
    consumers copy or rebuild what they touch).
    """
    root = Path(root) if root is not None else default_root()
    key = stable_hash(identity)
    kind = identity.get("kind", FORMAT)
    if kind in _HOT_KINDS:
        hot = _hot_read(root, key)
        if hot is not None:
            return hot[1]
    path = _pack_path(root, key)
    try:
        blob = path.read_bytes()
    except OSError:
        return None
    try:
        wrapper = pickle.loads(gzip.decompress(blob))
        if wrapper.get("format") != kind or wrapper.get("identity") != identity:
            return None
        _hot_store(root, key, kind, identity, wrapper["pack"])
        return wrapper["pack"]
    except Exception:
        # Corrupt payloads must never reach the causal path; drop the object.
        try:
            path.unlink()
        except OSError:
            pass
        return None


def put_pack(identity: dict, pack, *, root: Path | None = None,
             quota_bytes: int | None = None) -> Path:
    root = Path(root) if root is not None else default_root()
    key = stable_hash(identity)
    path = _pack_path(root, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = gzip.compress(pickle.dumps({"format": identity.get("kind", FORMAT),
                                       "identity": identity, "pack": pack},
                                      protocol=5), compresslevel=1)
    temporary = path.with_name(f"{key}.tmp-{os.getpid()}-{time.time_ns()}")
    temporary.write_bytes(blob)
    os.replace(temporary, path)
    _hot_store(root, key, identity.get("kind", FORMAT), identity, pack)
    _maybe_gc(root, quota_bytes if quota_bytes is not None else default_quota_bytes())
    return path


def get_or_build_with_identity(identity: dict, build, *,
                               root: Path | None = None,
                               quota_bytes: int | None = None):
    """Look up ``identity``, else call ``build()``, store and return its result.

    The second return value is 'cache' or 'compute' for caller telemetry.
    """
    payload = get_pack(identity, root=root)
    if payload is not None:
        return payload, "cache"
    payload = build()
    put_pack(identity, payload, root=root, quota_bytes=quota_bytes)
    return payload, "compute"


def chart_payload_identity(frame: pd.DataFrame, settings: dict) -> dict:
    """Identity of the unfiltered CzscSession chart payload for ``frame``.

    The frame must already be cut to the requested decision date; its content
    hash makes the decision date redundant. ``settings`` is strategy_config's
    output, whose version/engine fields cover the strategy config file and the
    vendored engine source tree. The display filter is applied per request, so
    one entry serves every chart start date.
    """
    return {
        "kind": CHART_FORMAT,
        "symbol": str(frame.iloc[0].symbol),
        "frame_sha256": frame_sha256(frame),
        "settings_sha256": stable_hash(settings),
        "code": _code_hashes(_CHART_CODE_FILES),
    }


def _chart_payload_of(obj):
    """Unwrap {"payload", "chart_state"} entries; bare payloads pass through."""
    if isinstance(obj, dict) and set(obj) == {"payload", "chart_state"}:
        return obj["payload"]
    return obj


def get_or_build_chart_payload(frame: pd.DataFrame, settings: dict, build, *,
                               root: Path | None = None,
                               quota_bytes: int | None = None):
    """Shared entry point for the unfiltered chart payload (session advance).

    ``build()`` must advance a CzscSession over ``frame`` and return
    ``session.payload(None)``; callers apply filter_chart_payload per request.
    Reads tolerate state-carrying entries written by get_or_build_chart.
    """
    payload, origin = get_or_build_with_identity(chart_payload_identity(frame, settings), build,
                                                 root=root, quota_bytes=quota_bytes)
    return _chart_payload_of(payload), origin


def get_or_build_chart(frame: pd.DataFrame, settings: dict, build, *,
                       root: Path | None = None, quota_bytes: int | None = None,
                       cancel_check=None):
    """Chart payload entry point with verified day-append continuation.

    ``build(frame, resume_state=None)`` advances a CzscSession and returns
    ``(session.payload(None), dump_chart_session(session))``. On a miss the
    newest stored strict-prefix entry for the same static identity (symbol,
    settings, code) is resumed after its frame hash is proven identical to
    the request frame's prefix; restore or advance doubts retry older
    candidates, then fall back to a full advance. The origin is 'cache',
    'compute_incremental' or 'compute'. Chart payloads are always complete
    (payload(None) is recomputed wholesale from the restored session), so no
    stitching is needed.
    """
    root = Path(root) if root is not None else default_root()
    identity = chart_payload_identity(frame, settings)
    payload = get_pack(identity, root=root)
    if payload is not None:
        return _chart_payload_of(payload), "cache"
    static = _static_identity(identity)
    for entry in _read_continuation(root, identity):
        rows = entry.get("rows")
        if not isinstance(rows, int) or not 0 < rows < len(frame):
            continue
        found = _read_pack_by_key(root, str(entry.get("key", "")))
        if found is None:
            continue
        old_identity, old_obj = found
        if _static_identity(old_identity) != static or not isinstance(old_obj, dict) or \
                set(old_obj) != {"payload", "chart_state"} or old_obj["chart_state"] is None:
            continue
        # The chart analog of the native raw-prefix guard: the candidate's
        # frame must be this request's exact observed prefix.
        if frame_sha256(frame.iloc[:rows]) != old_identity.get("frame_sha256"):
            continue
        try:
            payload, state = build(frame, resume_state=old_obj["chart_state"])
        except (KeyboardInterrupt, InterruptedError):
            raise
        except Exception:
            # Any resume doubt retries older candidates, then full advance.
            continue
        key = stable_hash(identity)
        put_pack(identity, {"payload": payload, "chart_state": state}, root=root, quota_bytes=quota_bytes)
        _write_continuation(root, identity, len(frame), key, identity["frame_sha256"])
        return payload, "compute_incremental"
    payload, state = build(frame, resume_state=None)
    key = stable_hash(identity)
    if state is None:
        put_pack(identity, payload, root=root, quota_bytes=quota_bytes)
    else:
        put_pack(identity, {"payload": payload, "chart_state": state}, root=root, quota_bytes=quota_bytes)
        _write_continuation(root, identity, len(frame), key, identity["frame_sha256"])
    return payload, "compute"


def features_frame_identity(frame: pd.DataFrame, calendar_dates, profile: str) -> dict:
    """Identity of the prepared feature frame for (raw, calendar, profile).

    ``frame`` is the liquidity-augmented raw frame build_features reads. The
    settings hash pins the strategy config build_features consumes through
    strategy_config(None), including the vendored engine source tree.
    """
    from my_strategy.services.czsc_analysis import strategy_config
    return {
        "kind": FEATURES_FORMAT,
        "symbol": str(frame.iloc[0].symbol),
        "frame_sha256": frame_sha256(frame),
        "calendar_sha256": stable_hash(list(calendar_dates)),
        "profile": str(profile),
        "settings_sha256": stable_hash(strategy_config(None)),
        "code": _code_hashes(_FEATURES_CODE_FILES),
    }


def _features_frame_of(obj):
    """Unwrap a features entry; legacy entries are bare prepared frames."""
    if isinstance(obj, dict) and set(obj) == {"prepared", "features_state"}:
        return obj["prepared"]
    return obj


def get_or_build_features(frame: pd.DataFrame, calendar_dates, profile: str, build, *,
                          root: Path | None = None,
                          quota_bytes: int | None = None):
    """Shared entry point for prepare_observed_data's feature frame, with
    verified day-append continuation.

    ``build(frame, resume_state=None)`` must return ``(prepared, state)`` —
    the profile/wyckoff-prepared features DataFrame (before raw-only columns
    are copied across) and the build_features incremental state, or None for
    stubs. On a miss the newest stored strict-prefix entry for the same static
    identity (symbol, calendar, profile, settings, code) is resumed after its
    frame hash is proven identical to the request frame's prefix; the closure
    advances build_features over only the appended rows, stitches onto the
    stored prefix frame and reruns the cheap deterministic post-pipeline over
    the stitched frame. Any resume doubt retries older candidates, then falls
    back to a full build: a wrong continuation can never be served. The origin
    is 'cache', 'compute_incremental' or 'compute'.
    """
    root = Path(root) if root is not None else default_root()
    identity = features_frame_identity(frame, calendar_dates, profile)
    cached = get_pack(identity, root=root)
    if cached is not None:
        return _features_frame_of(cached), "cache"
    static = _static_identity(identity)
    for entry in _read_continuation(root, identity):
        rows = entry.get("rows")
        if not isinstance(rows, int) or not 0 < rows < len(frame):
            continue
        found = _read_pack_by_key(root, str(entry.get("key", "")))
        if found is None:
            continue
        old_identity, old_obj = found
        if _static_identity(old_identity) != static or not isinstance(old_obj, dict) or \
                set(old_obj) != {"prepared", "features_state"} or old_obj["features_state"] is None:
            continue
        # Same guard as the chart/native resumptions: the candidate's frame
        # must be this request's exact observed prefix.
        if frame_sha256(frame.iloc[:rows]) != old_identity.get("frame_sha256"):
            continue
        try:
            prepared, state = build(frame, resume_state=old_obj)
        except (KeyboardInterrupt, InterruptedError):
            raise
        except Exception:
            # Any resume doubt retries older candidates, then full build.
            continue
        key = stable_hash(identity)
        put_pack(identity, {"prepared": prepared, "features_state": state}, root=root, quota_bytes=quota_bytes)
        _write_continuation(root, identity, len(frame), key, identity["frame_sha256"])
        return prepared, "compute_incremental"
    prepared, state = build(frame, resume_state=None)
    key = stable_hash(identity)
    if state is None:
        put_pack(identity, prepared, root=root, quota_bytes=quota_bytes)
    else:
        put_pack(identity, {"prepared": prepared, "features_state": state}, root=root, quota_bytes=quota_bytes)
        _write_continuation(root, identity, len(frame), key, identity["frame_sha256"])
    return prepared, "compute"


def get_or_build(frame: pd.DataFrame, calendar_dates, build, *,
                 root: Path | None = None, quota_bytes: int | None = None) -> tuple[dict, str]:
    """Shared entry point: canonicalize, look up, else build and store.

    ``build`` receives the canonical frame and must return the pack. The
    second return value is 'cache' or 'compute' for caller telemetry.
    """
    canonical = canonical_frame(frame)
    identity = native_pack_identity(canonical, calendar_dates)
    return get_or_build_with_identity(identity, lambda: build(canonical),
                                      root=root, quota_bytes=quota_bytes)


# --- Verified incremental continuation -------------------------------------
#
# A pack built with capture_state=True carries build_native_evidence's
# explicit incremental_state (native_evidence_state_v2): retained native
# session bars with geometry SHA, event state machines, prefix/calendar
# hashes. A per-symbol continuation index maps everything in the identity
# except the frame hash to recent (rows -> pack key) entries. On a miss the
# newest strict-prefix candidate resumes the build over only the appended
# bars and stitches its delta onto the stored prefix pack. Every resume is
# re-validated inside build_native_evidence (schema/config/raw-prefix/
# calendar-prefix SHAs) and any doubt falls back to a full replay: a wrong
# continuation can never be served, only a slower correct one.

_CONTINUATION_DIR = "continuation"
_CONTINUATION_KEEP = 4


def _static_identity(identity: dict) -> dict:
    return {key: value for key, value in identity.items() if key != "frame_sha256"}


def _continuation_path(root: Path, identity: dict) -> Path:
    return root / _CONTINUATION_DIR / f"{stable_hash(_static_identity(identity))}.json"


def _read_continuation(root: Path, identity: dict) -> list[dict]:
    try:
        data = json.loads(_continuation_path(root, identity).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict) or data.get("static") != _static_identity(identity):
        return []
    entries = [entry for entry in data.get("entries", []) if isinstance(entry, dict)]
    entries.sort(key=lambda entry: entry.get("rows", 0), reverse=True)
    return entries


def _write_continuation(root: Path, identity: dict, rows: int, key: str, frame_sha256: str) -> None:
    static = _static_identity(identity)
    entries = [entry for entry in _read_continuation(root, identity)
               if entry.get("rows") != rows and _pack_path(root, str(entry.get("key", ""))).is_file()]
    entries.insert(0, {"rows": int(rows), "key": key, "frame_sha256": frame_sha256})
    entries = entries[:_CONTINUATION_KEEP]
    path = _continuation_path(root, identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.stem}.tmp-{os.getpid()}-{time.time_ns()}")
    temporary.write_text(json.dumps({"static": static, "entries": entries},
                                    ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _read_pack_by_key(root: Path, key: str) -> tuple[dict, dict] | None:
    """Read a pack by content address, verifying identity hashes to that key."""
    hot = _hot_read(root, key)
    if hot is not None:
        return hot
    path = _pack_path(root, key)
    try:
        wrapper = pickle.loads(gzip.decompress(path.read_bytes()))
        identity = wrapper["identity"]
        if stable_hash(identity) != key:
            raise ValueError("pack identity does not match its content address")
        _hot_store(root, key, identity.get("kind", FORMAT), identity, wrapper["pack"])
        return identity, wrapper["pack"]
    except (OSError, ValueError, KeyError, pickle.UnpicklingError):
        try:
            path.unlink()
        except OSError:
            pass
        return None


def _stitch_native_pack(old: dict, delta: dict, prefix_rows: int) -> dict:
    """Prefix pack + resumed delta == one full build (incremental contract).

    tests/test_incremental_native_state.py proves frame/events/evaluations
    equality of resumed deltas against full replays; stitching concatenates
    the disjoint date ranges along exactly those fields.
    """
    if len(old["frame"]) != prefix_rows:
        raise ValueError("stored prefix pack row count drifted from its continuation state")
    # Slices of one observed frame keep its index labels; concatenating without
    # reindexing reproduces the full build's index exactly.
    frame = pd.concat([old["frame"], delta["frame"]])
    frame.attrs.update(delta["frame"].attrs)
    pack = {**delta, "frame": frame,
            "events": old["events"] + delta["events"],
            "evaluations": old["evaluations"] + delta["evaluations"],
            "wyckoff_events": old["wyckoff_events"] + delta["wyckoff_events"],
            "ict_events": old["ict_events"] + delta["ict_events"],
            "auxiliary_evaluations": old["auxiliary_evaluations"] + delta["auxiliary_evaluations"]}
    pack["events"] = sorted(pack["events"], key=lambda item: (item["date"], item["source"], item["event_id"]))
    return pack


def _slice_native_pack(old: dict, canonical: pd.DataFrame) -> dict:
    """Serve an earlier cutoff from a longer pack, content-verified.

    Causality makes a pack's first N rows identical to a fresh build over the
    first N input rows (the same property the incremental contract proves).
    The slice is verified against the requested frame content before serving,
    is never stored, and carries no continuation state.
    """
    rows = len(canonical)
    frame = old["frame"].iloc[:rows]
    if frame_sha256(frame[list(canonical.columns)]) != frame_sha256(canonical):
        raise ValueError("stored longer pack prefix content differs from the request")
    cutoff = str(frame["date"].iloc[-1])[:10]
    keep = {"frame": frame,
            "events": [item for item in old["events"] if item["date"] <= cutoff],
            "evaluations": [item for item in old["evaluations"] if item["date"] <= cutoff],
            "wyckoff_events": [item for item in old["wyckoff_events"] if item["date"] <= cutoff],
            "ict_events": [item for item in old["ict_events"] if item["date"] <= cutoff],
            "auxiliary_evaluations": [item for item in old["auxiliary_evaluations"] if item["date"] <= cutoff]}
    return {key: keep.get(key, old[key]) for key in old if key not in ("incremental_state", "incremental")}


def get_or_build_native(frame: pd.DataFrame, calendar_dates, *,
                        root: Path | None = None, quota_bytes: int | None = None,
                        cancel_check=None) -> tuple[dict, str]:
    """Native pack entry point with verified day-append continuation.

    On a miss the newest stored strict-prefix pack for the same static
    identity (symbol, calendar, contract, code) resumes through
    build_native_evidence's incremental_state; only the appended bars are
    advanced. Resume failures (prefix drift, state corruption, schema or code
    change) retry older candidates, then fall back to a full replay. The
    origin is 'cache', 'compute_incremental' or 'compute'.
    """
    from my_strategy.services.czsc_native_evidence import build_native_evidence
    root = Path(root) if root is not None else default_root()
    canonical = canonical_frame(frame)
    identity = native_pack_identity(canonical, calendar_dates)
    payload = get_pack(identity, root=root)
    if payload is not None:
        return payload, "cache"
    longer = sorted((entry for entry in _read_continuation(root, identity)
                     if isinstance(entry.get("rows"), int) and entry["rows"] > len(canonical)),
                    key=lambda entry: entry["rows"])
    for entry in longer:
        found = _read_pack_by_key(root, str(entry.get("key", "")))
        if found is None:
            continue
        old_identity, old_pack = found
        if _static_identity(old_identity) != _static_identity(identity):
            continue
        try:
            return _slice_native_pack(old_pack, canonical), "cache_prefix"
        except (ValueError, KeyError, TypeError):
            continue
    for entry in _read_continuation(root, identity):
        rows = entry.get("rows")
        if not isinstance(rows, int) or not 0 < rows < len(canonical):
            continue
        found = _read_pack_by_key(root, str(entry.get("key", "")))
        if found is None:
            continue
        old_identity, old_pack = found
        if _static_identity(old_identity) != _static_identity(identity):
            continue
        state = old_pack.get("incremental_state")
        if state is None:
            continue
        try:
            delta = build_native_evidence(canonical, market_dates=calendar_dates,
                                          incremental_state=state, capture_state=True,
                                          cancel_check=cancel_check)
            if delta["incremental"]["native_prefix_reused"] != rows:
                continue
            payload = _stitch_native_pack(old_pack, delta, rows)
        except (KeyboardInterrupt, InterruptedError):
            raise
        except Exception:
            # Any resume doubt retries older candidates, then full replay.
            continue
        key = stable_hash(identity)
        put_pack(identity, payload, root=root, quota_bytes=quota_bytes)
        _write_continuation(root, identity, len(canonical), key, identity["frame_sha256"])
        return payload, "compute_incremental"
    payload = build_native_evidence(canonical, market_dates=calendar_dates,
                                    capture_state=True, cancel_check=cancel_check)
    key = stable_hash(identity)
    put_pack(identity, payload, root=root, quota_bytes=quota_bytes)
    _write_continuation(root, identity, len(canonical), key, identity["frame_sha256"])
    return payload, "compute"


def gc_native_packs(*, root: Path | None = None, quota_bytes: int | None = None) -> dict:
    """Evict oldest packs until the store is within quota; sweep stale tmp files."""
    root = Path(root) if root is not None else default_root()
    if quota_bytes is None:
        quota_bytes = default_quota_bytes()
    if not root.is_dir():
        return {"root": str(root), "removed": 0, "freed_bytes": 0, "kept_bytes": 0}
    entries = []
    now = time.time()
    removed = freed = 0
    for path in root.glob("*/*.pkl.gz"):
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((stat.st_mtime, stat.st_size, path))
    for path in root.glob("*/*.tmp-*"):
        try:
            if now - path.stat().st_mtime > _TMP_MAX_AGE_SECONDS:
                freed += path.stat().st_size
                path.unlink()
                removed += 1
        except OSError:
            pass
    total = sum(size for _, size, _ in entries)
    if total > quota_bytes:
        entries.sort()
        for _, size, path in entries:
            if total <= quota_bytes * 0.9:
                break
            try:
                path.unlink()
            except OSError:
                continue
            total -= size
            freed += size
            removed += 1
    # Drop shard directories left empty by evictions.
    for shard in root.iterdir():
        try:
            if shard.is_dir() and not any(shard.iterdir()):
                shard.rmdir()
        except OSError:
            pass
    return {"root": str(root), "removed": removed, "freed_bytes": freed, "kept_bytes": total}


def _maybe_gc(root: Path, quota_bytes: int) -> None:
    global _last_gc_monotonic
    now = time.monotonic()
    if now - _last_gc_monotonic < _GC_INTERVAL_SECONDS:
        return
    _last_gc_monotonic = now
    gc_native_packs(root=root, quota_bytes=quota_bytes)
