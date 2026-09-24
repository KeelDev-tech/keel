"""Hash-chained append-only log.

Every record carries seq, prev_hash, and hash = sha256(canonical_json(
record-without-hash-fields)). verify() replays the whole chain and
reports the first broken link. Tampering with any record — or deleting
one — breaks the chain deterministically.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

GENESIS_PREV = "0" * 64


def canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def record_hash(seq: int, prev_hash: str, body: dict) -> str:
    payload = {"seq": seq, "prev_hash": prev_hash, "body": body}
    return hashlib.sha256(canonical(payload)).hexdigest()


class HashChain:
    """In-memory hash chain. Subclassed by the file-backed ledger."""

    def __init__(self):
        self._records: list[dict] = []

    def append(self, body: dict) -> dict:
        seq = len(self._records)
        prev = self._records[-1]["hash"] if self._records else GENESIS_PREV
        rec = {"seq": seq, "prev_hash": prev,
               "recorded_at": datetime.now(timezone.utc).isoformat(),
               "body": body}
        rec["hash"] = record_hash(seq, prev, body)
        self._records.append(rec)
        return rec

    def verify(self) -> tuple[bool, str]:
        """Replay the chain. Returns (ok, detail)."""
        prev = GENESIS_PREV
        for i, rec in enumerate(self._records):
            if rec.get("seq") != i:
                return False, f"seq break at index {i}"
            if rec.get("prev_hash") != prev:
                return False, f"prev_hash mismatch at seq {i}"
            want = record_hash(rec["seq"], rec["prev_hash"], rec["body"])
            if rec.get("hash") != want:
                return False, f"hash mismatch at seq {i} — tampered record"
            prev = rec["hash"]
        return True, f"chain ok: {len(self._records)} records"

    def __len__(self) -> int:
        return len(self._records)

    def records(self) -> list[dict]:
        return list(self._records)
