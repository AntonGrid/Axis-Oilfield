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
    location: Optional[str] = None


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
