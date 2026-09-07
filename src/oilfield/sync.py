"""Offline outbox — events signed at the pad, synced later to the registry.

Mirrors Axis ADR-0001 (the key never leaves the device) and SPEC §7 (offline
operation): the storekeeper's scanner signs events locally, stores them in an
outbox, and when the network returns they are applied to the gateway registry.
Sync is **idempotent**: duplicates (replay / already-seen nonces) are skipped,
never double-applied. A ``.json`` file may be used so the outbox survives a
process restart on the pad.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from oilfield.custody import InventoryRegistry
from oilfield.model import InventoryEvent, InventorySnapshot


@dataclass
class SyncResult:
    synced: int = 0
    skipped: int = 0
    rejected: int = 0
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "synced": self.synced,
            "skipped": self.skipped,
            "rejected": self.rejected,
            "reasons": self.reasons[:5],
        }


class EventOutbox:
    """Local queue of signed artefacts waiting to reach the gateway."""

    def __init__(self, device_id: str = "") -> None:
        self.device_id = device_id
        #: each entry: {"kind": "event"|"snapshot", "data": {...}}
        self._pending: List[Dict[str, Any]] = []

    # ── building the queue ───────────────────────────────────────────────────

    def add(self, event: InventoryEvent) -> None:
        self._pending.append({"kind": "event", "data": event.to_dict()})

    def add_snapshot(self, snapshot: InventorySnapshot) -> None:
        self._pending.append({"kind": "snapshot", "data": snapshot.to_dict()})

    def pending(self) -> List[Dict[str, Any]]:
        return list(self._pending)

    def __len__(self) -> int:
        return len(self._pending)

    # ── persistence (survives a pad restart) ─────────────────────────────────

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                {"device_id": self.device_id, "pending": self._pending},
                fh,
                ensure_ascii=False,
            )

    @classmethod
    def load(cls, path: str) -> "EventOutbox":
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        box = cls(device_id=data.get("device_id", ""))
        box._pending = data.get("pending", [])
        return box

    # ── sync ─────────────────────────────────────────────────────────────────

    def sync(
        self,
        registry: InventoryRegistry,
        *,
        verify: Optional[Callable[[Any], tuple]] = None,
        facts: Optional[Dict[str, Any]] = None,
    ) -> SyncResult:
        """Apply all pending artefacts to the gateway registry.

        - ``verify`` is the Axis Core verifier injected by the caller
          (the domain never verifies crypto itself).
        - Accepted artefacts are removed; replays are removed and counted as
          skipped; policy rejections stay in the outbox for inspection.
        """
        if verify is not None:
            registry.facts.setdefault("verify", verify)
            registry.facts.setdefault("verify_snapshot", verify)

        result = SyncResult()
        kept: List[Dict[str, Any]] = []
        for entry in self._pending:
            kind = entry["kind"]
            data = entry["data"]
            if kind == "snapshot":
                snap = InventorySnapshot.from_dict(data)
                outcome = registry.record_snapshot(snap, **({"verify_signature": True} if verify else {}))
            else:
                event = InventoryEvent.from_dict(data)
                outcome = registry.apply(event, facts=facts or {})
            if outcome.accepted:
                result.synced += 1
            elif "replay" in outcome.reason:
                result.skipped += 1
            else:
                result.rejected += 1
                result.reasons.append(f"{kind}:{outcome.reason}")
                kept.append(entry)
        self._pending = kept
        return result
