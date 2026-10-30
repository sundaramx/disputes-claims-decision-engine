"""Immutable decision ledger.

Every case-affecting event — automated or human, decision or notice or clock
transition — is appended here with a SHA-256 hash chain (paper §4: "in a Reg E
examination the question is not only whether the outcome was right but whether the
institution can reconstruct why").

Self-containment (critique finding #10): the canonical JSON payload lives IN the
ledger row, so a case history is reconstructable from the ledger alone.

    hash = SHA256(prev_hash || seq || ts_iso || event_type || canonical_payload)
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime

from app.store.db import Store

GENESIS = "0" * 64


def canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(prev_hash: str, seq: int, ts_iso: str, event_type: str,
                 canonical_payload: str) -> str:
    material = f"{prev_hash}|{seq}|{ts_iso}|{event_type}|{canonical_payload}"
    return hashlib.sha256(material.encode()).hexdigest()


class DecisionLedger:
    def __init__(self, store: Store):
        self._store = store
        self._lock = threading.Lock()

    def append(self, ts: datetime, case_id: str, event_type: str, payload: dict) -> int:
        with self._lock:
            last = self._store.last_ledger_row()
            prev_hash = last["hash"] if last else GENESIS
            seq = (last["seq"] + 1) if last else 1
            ts_iso = ts.isoformat()
            cp = canonical(payload)
            h = compute_hash(prev_hash, seq, ts_iso, event_type, cp)
            payload_hash = hashlib.sha256(cp.encode()).hexdigest()
            return self._store.append_ledger(ts, case_id, event_type, cp,
                                             payload_hash, prev_hash, h)

    def verify(self) -> dict:
        """Walk the full chain; recompute every hash. Returns a verification report."""
        prev = GENESIS
        checked = 0
        for row in self._store.ledger_entries():
            cp = canonical(row.payload)
            expected_payload_hash = hashlib.sha256(cp.encode()).hexdigest()
            expected = compute_hash(prev, row.seq, row.ts.isoformat(),
                                    row.event_type, cp)
            if row.prev_hash != prev or row.hash != expected \
                    or row.payload_hash != expected_payload_hash:
                return {"valid": False, "entries": checked, "broken_at_seq": row.seq}
            prev = row.hash
            checked += 1
        return {"valid": True, "entries": checked, "head_hash": prev}
