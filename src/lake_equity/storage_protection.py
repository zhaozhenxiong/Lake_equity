"""Protect retained learning/read identities before lake lifecycle maintenance."""
from __future__ import annotations

import json
from pathlib import Path
import re
import threading

from cnequity.domain.datasets import DATASETS
from cnequity.storage.lifecycle import LifecycleError, reference_files, reference_fingerprint
from cnequity.serve.storage import StorageMaintenance
from lake_equity.market.store import MarketStore


class LearningReferences:
    def __init__(self, root, lifecycle):
        self.root, self.lifecycle = Path(root).resolve(), lifecycle
        self.owned = [self.root / relative for relative in
                      ("var/learning", "var/market_views", "var/research_inputs", "var/research")]

    def overview(self):
        """Display retained read identities; deletion still requires full collect()."""
        store = MarketStore(self.root)
        roots = [{"path": str(store.reads_root), "exclude": []}] if store.reads_root.is_dir() else []
        refs, revisions = set(), set()
        for path in reference_files(roots):
            if path.suffix != ".json":
                continue
            snapshot = store.read_snapshot(path.stem)
            refs.add(snapshot["read_ref"])
            revisions.update(snapshot["revision_map"].items())
        holds = {f"revision/{name}/{rid}": ["retained_lake_equity_learning_or_read_identity"]
                 for name, rid in sorted(revisions)}
        registered = (self.lifecycle.registry() or {}).get("holds", {})
        protected = set(holds) | {oid for oid, reasons in registered.items()
                                  if oid.startswith("revision/") and reasons}
        return {"holds": holds, "read_ref_count": len(refs), "protected_revision_count": len(protected),
                "reference_fingerprint": None, "full_reference_inventory": "required_at_review",
                "protection_scope": "retained_read_manifests_and_registered_holds", "deletion_approved": False}

    def collect(self, *, create=False):
        if create:
            for path in self.owned:
                path.mkdir(parents=True, exist_ok=True)
        roots = list((self.lifecycle.registry() or {}).get("reference_roots", []))
        known = {str(Path(item["path"]).resolve()) for item in roots}
        for path in self.owned:
            if path.is_dir() and str(path) not in known:
                roots.append({"path": str(path), "exclude": []})
        before = reference_fingerprint(roots, meta_root=self.lifecycle.meta)
        owned = [{"path": str(path), "exclude": []} for path in self.owned if path.is_dir()]
        refs, revisions = set(), set()

        def inspect(value, dataset=None):
            if isinstance(value, list):
                for item in value:
                    inspect(item, dataset)
            elif isinstance(value, dict):
                for key, item in value.items():
                    if key.endswith("read_ref") and item is not None:
                        if not isinstance(item, str) or re.fullmatch(r"[a-f0-9]{64}", item) is None:
                            raise LifecycleError("Invalid retained learning read reference")
                        refs.add(item)
                    if key == "revision_map" and isinstance(item, dict):
                        for name, rid in item.items():
                            if name in DATASETS and isinstance(rid, str) and re.fullmatch(r"[a-f0-9]{32}", rid):
                                revisions.add((name, rid))
                    if key == "revision_id" and dataset in DATASETS and isinstance(item, str) and re.fullmatch(r"[a-f0-9]{32}", item):
                        revisions.add((dataset, item))
                    inspect(item, key if key in DATASETS else dataset)

        for path in reference_files(owned):
            if path.suffix == ".json":
                try:
                    inspect(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, ValueError) as error:
                    raise LifecycleError(f"Cannot verify retained research references: {path.name}") from error
        store = MarketStore(self.root)
        for read_ref in sorted(refs):
            snapshot = store.read_snapshot(read_ref)
            for name, rid in snapshot["revision_map"].items():
                revisions.add((name, rid))
        after = reference_fingerprint(roots, meta_root=self.lifecycle.meta)
        if before != after:
            raise LifecycleError("Research references changed during inventory; preview again")
        holds = {f"revision/{name}/{rid}": ["retained_lake_equity_learning_or_read_identity"]
                 for name, rid in sorted(revisions)}
        return {"schema_version": 1, "meta_root": str(self.lifecycle.meta), "reference_roots": roots,
                "reference_fingerprint": before, "holds": holds, "experiments": [], "cases": [],
                "read_ref_count": len(refs), "protected_revision_count": len(holds)}

    def refresh(self):
        proof = self.collect(create=True)
        self.lifecycle.import_manifest(proof)
        return proof


class ProtectedStorageMaintenance(StorageMaintenance):
    def __init__(self, root, config, *, gate, active):
        super().__init__(config)
        self.gate, self.active = gate, active
        self.references = LearningReferences(root, self.store)
        self._reference_reviews = {}
        self._reference_lock = threading.Lock()

    def summary(self):
        result = super().summary()
        proof = self.references.overview()
        for item in result.get("objects", []):
            if item.get("object_id") in proof["holds"]:
                item["status"] = "protected"
                item["holds"] = list(dict.fromkeys(item.get("holds", []) + proof["holds"][item["object_id"]]))
                item["blocked_reasons"] = list(dict.fromkeys(item.get("blocked_reasons", []) + ["learning_reference"]))
        result["learning_protection"] = {key: value for key, value in proof.items() if key != "holds"}
        return result

    def review(self, kind, phase="purge", resume_plan_id=None, object_ids=None):
        with self._reference_lock:
            if self.busy or self.gate.closed:
                raise LifecycleError("A storage operation is already active")
            proof = self.references.refresh()
            result = super().review(kind, phase, resume_plan_id, object_ids)
            self._reference_reviews[result["job_id"]] = proof["reference_fingerprint"]
            return result

    def confirm(self, review_id, confirmation_token, *, confirmed, maintenance):
        with self._reference_lock:
            if self.busy or self.gate.closed:
                raise LifecycleError("A storage operation is already active")
            if self.active():
                raise LifecycleError("Stop active research/data jobs before confirming storage maintenance")
            self.gate.close()
            try:
                self.gate.drain()
                expected = self._reference_reviews.get(review_id)
                current = self.references.collect()
                if expected is None or current["reference_fingerprint"] != expected:
                    raise LifecycleError("Research references changed after preview; review the exact objects again")
                return super().confirm(review_id, confirmation_token, confirmed=confirmed, maintenance=maintenance)
            except BaseException:
                self.gate.reopen()
                raise
