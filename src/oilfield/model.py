"""Domain entities (Axis-Oilfield)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Site:
    """A physical site: central warehouse or a remote pad («куст»)."""

    site_id: str
    name: str
    region: str = ""


@dataclass
class Location:
    """An addressable storage unit within a site.

    ``location_id`` IS the full address: ``site → container → shelf → cell``
    (e.g. ``PAD-12:container-B:shelf-3:cell-7``). ``site_id`` is kept as a
    separate filter field.
    """

    location_id: str
    site_id: str
    kind: str = "area"  # container | shelf | cell | pallet | area
    parent: Optional[str] = None

    @property
    def address(self) -> str:
        return self.location_id


@dataclass
class Item:
    """One physical unit or batch of MTR."""

    item_id: str
    sku: str
    serial_no: Optional[str] = None
    batch_no: Optional[str] = None
    qty: float = 1.0
    cert_ids: List[str] = field(default_factory=list)
    #: Address this item is currently bound to (filled by the registry).
    #: Special values: ``""`` = at the supplier (not received yet),
    #: ``"issued:<well>"`` = issued to a well/crew and no longer in storage.
    location: Optional[str] = None

    #: Physical condition label (e.g. ``"ok"``, ``"damaged"``, ``"quarantine"``).
    status: str = "ok"

    @property
    def is_issued(self) -> bool:
        return bool(self.location and self.location.startswith("issued:"))


@dataclass
class Certificate:
    """Quality document (pipe certificate, chemical passport).

    The content hash lets anyone verify the document was not swapped:
    the certificate registry stores the hash, the paper/PDF stays in 1C.
    """

    cert_id: str
    sku: str
    content_hash: str = ""
    issuer: str = ""
    issue_date: str = ""
    valid_until: str = ""


@dataclass
class InventorySnapshot:
    """A signed observation of reality at one location during inventory.

    This is the *proof artefact* of an inventory count: the storekeeper
    signs what he actually saw (item_id / sku / qty per cell). Later the
    registry compares the signed snapshot with its own expectations and
    produces the пересортица report.
    """

    snapshot_id: str
    location_id: str
    actor: str  # base64 public key of the storekeeper
    ts: str = ""
    nonce: int = 0
    #: ``{item_id: {"sku": .., "qty": .., "serial_no": ..}}`` observed reality.
    observations: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    signature: str = ""

    def to_payload(self) -> Dict[str, Any]:
        return {
            "algo": "ed25519",
            "device_id": self.actor,
            "nonce": str(self.nonce),
            "timestamp": self.ts,
            "payload": {
                "event_type": "inventory_snapshot",
                "location_id": self.location_id,
                "observations": [
                    {"item_id": k, **v} for k, v in sorted(self.observations.items())
                ],
            },
        }

    def to_dict(self) -> Dict[str, Any]:
        d = self.to_payload()
        d["payload"]["signature"] = self.signature
        d["snapshot_id"] = self.snapshot_id
        return d

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "InventorySnapshot":
        pl = raw.get("payload", raw)
        observations = {}
        for obs in pl.get("observations", []):
            item_id = obs.pop("item_id")
            observations[item_id] = obs
        return cls(
            snapshot_id=raw.get("snapshot_id", pl.get("snapshot_id", "")),
            location_id=pl.get("location_id", ""),
            actor=raw.get("device_id", ""),
            ts=raw.get("timestamp", ""),
            nonce=int(raw.get("nonce", 0) or 0),
            observations=observations,
            signature=pl.get("signature", ""),
        )


@dataclass
class InventoryEvent:

    """A signed inventory movement.

    The canonical JSON of ``to_payload()`` is what the actor signs with
    Ed25519 (see Axis Core ``signature_utils``). ``signature`` is filled by
    the trust layer, not by the domain logic.
    """

    event_type: str  # arrival | receive | store | issue | return | inventory
    item_id: str
    sku: str
    actor: str  # base64 public key of the storekeeper/scanner
    from_ref: str = ""
    to_ref: str = ""
    qty: float = 1.0
    ts: str = ""
    nonce: int = 0
    certificates: List[str] = field(default_factory=list)
    signature: str = ""

    def to_payload(self) -> Dict[str, Any]:
        """Payload covered by the signature (canonical, domain-agnostic)."""
        return {
            "algo": "ed25519",
            "device_id": self.actor,
            "nonce": str(self.nonce),
            "timestamp": self.ts,
            "payload": {
                "event_type": self.event_type,
                "item_id": self.item_id,
                "sku": self.sku,
                "from": self.from_ref,
                "to": self.to_ref,
                "qty": self.qty,
                "certificates": list(self.certificates),
            },
        }

    def to_dict(self) -> Dict[str, Any]:
        d = self.to_payload()
        d["payload"]["signature"] = self.signature
        return d

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "InventoryEvent":
        """Rebuild an event from :meth:`to_dict` (wire format for outbox sync)."""
        pl = raw.get("payload", raw)
        return cls(
            event_type=pl.get("event_type", ""),
            item_id=pl.get("item_id", ""),
            sku=pl.get("sku", ""),
            actor=raw.get("device_id", pl.get("device_id", "")),
            from_ref=pl.get("from", ""),
            to_ref=pl.get("to", ""),
            qty=pl.get("qty", 1.0),
            ts=raw.get("timestamp", pl.get("timestamp", "")),
            nonce=int(raw.get("nonce", pl.get("nonce", 0)) or 0),
            certificates=list(pl.get("certificates", [])),
            signature=pl.get("signature", ""),
        )
