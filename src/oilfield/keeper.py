"""Axis Core bridge — a storekeeper who signs and a gateway that verifies.

The pure domain (``oilfield``) never imports ``axis_core``; this module is the
only place that touches the crypto layer. It mirrors the ADR-0001 pattern:
the private key lives on the storekeeper's device and never leaves it — only
the base64 public key and signatures travel.

Importing this module requires ``axis-core`` on ``PYTHONPATH``.
"""

from __future__ import annotations

from typing import Any, Tuple

from oilfield.model import InventoryEvent, InventorySnapshot

# ── Axis Core trust layer ────────────────────────────────────────────────────
from axis_core.signature_utils import (
    canonical_proof_message,
    encode_public_key,
    generate_device_key,
    sign_proof,
    verify_ed25519_signature,
)


def _payload(obj: Any) -> dict:
    if isinstance(obj, InventoryEvent):
        return obj.to_payload()
    if isinstance(obj, InventorySnapshot):
        return obj.to_payload()
    raise TypeError(f"unsupported artefact: {type(obj)!r}")


class Storekeeper:
    """A storekeeper with an Ed25519 key — signs events and snapshots."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._key = generate_device_key()
        self.public_key_b64 = encode_public_key(self._key)
        self.nonce = 0

    def next_nonce(self) -> int:
        self.nonce += 1
        return self.nonce

    def _stamp(self, obj: Any) -> None:
        obj.actor = self.public_key_b64
        obj.nonce = self.next_nonce()
        import datetime as dt

        obj.ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    def sign(self, event: InventoryEvent) -> InventoryEvent:
        """Sign an inventory event; returns it with the signature filled."""
        self._stamp(event)
        event.signature = sign_proof(self._key, event.to_payload())
        return event

    def sign_snapshot(self, snapshot: InventorySnapshot) -> InventorySnapshot:
        self._stamp(snapshot)
        snapshot.signature = sign_proof(self._key, snapshot.to_payload())
        return snapshot


class Gateway:
    """The receiving side: verifies any signed artefact (oracle/gateway)."""

    def verify(self, obj: Any) -> Tuple[bool, str]:
        message = canonical_proof_message(_payload(obj))
        ok = verify_ed25519_signature(obj.actor, message, obj.signature)
        return (ok, "ok" if ok else "signature_invalid")
