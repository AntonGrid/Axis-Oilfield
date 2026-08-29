"""Axis-Oilfield end-to-end demo — MTR chain-of-custody on Axis Core.

Story (real oil-field scenario):

  a storekeeper receives a batch of tubing (НКТ-73), places it into an
  addressable cell, moves part of it to a remote pad («куст 12»), then a
  work order arrives for the *wrong* SKU — the policy blocks it (пересортица).
  An inventory check finds a discrepancy, and a custody-break anomaly is
  flagged when someone claims the pipe is on pad 5 while the last signed
  event says pad 12. The last event is signed offline and synced later.

Run:  python examples/oilfield_inventory.py
Requires: axis-core on PYTHONPATH (pip install -e ../Axis-core)
"""
from __future__ import annotations

import datetime as dt

from oilfield import InventoryRegistry
from oilfield.model import InventoryEvent, Item

# ── Axis Core trust layer ────────────────────────────────────────────────────
from axis_core.signature_utils import (
    canonical_proof_message,
    encode_public_key,
    generate_device_key,
    sign_proof,
    verify_ed25519_signature,
)


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Storekeeper:
    """A storekeeper with an Ed25519 key — signs every event (ADR-0001)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._key = generate_device_key()
        self.public_key_b64 = encode_public_key(self._key)
        self.nonce = 0

    def next_nonce(self) -> int:
        self.nonce += 1
        return self.nonce

    def sign(self, event: InventoryEvent) -> InventoryEvent:
        """Sign the canonical payload; returns the event with ``signature``."""
        event.actor = self.public_key_b64
        event.nonce = self.next_nonce()
        event.ts = _now_iso()
        payload = event.to_payload()
        message = canonical_proof_message(payload)
        event.signature = sign_proof(self._key, payload)
        return event

    def verify(self, event: InventoryEvent) -> tuple:
        """Verify an event's signature (the oracle/gateway side)."""
        message = canonical_proof_message(event.to_payload())
        ok = verify_ed25519_signature(event.actor, message, event.signature)
        return (ok, "ok" if ok else "signature_invalid")


def setup_registry() -> tuple:
    """Sites + addressable locations + items on the central warehouse."""
    reg = InventoryRegistry()
    reg.add_site("WH-01", "Central warehouse", "Base")
    reg.add_site("PAD-5", "Well pad 5", "Field")
    reg.add_site("PAD-12", "Well pad 12", "Field")

    # Addressable storage: site → container → shelf → cell.
    reg.add_location("WH-01:row-A:shelf-1:cell-1", "WH-01", "cell")
    reg.add_location("WH-01:row-A:shelf-1:cell-2", "WH-01", "cell")
    reg.add_location("PAD-5:container-B:pallet-2", "PAD-5", "pallet")
    reg.add_location("PAD-12:container-B:shelf-3:cell-7", "PAD-12", "cell")

    # Three tubing units (НКТ-73) with a quality certificate.
    for i in range(3):
        reg.add_item(
            Item(
                item_id=f"tube-{i + 1:02d}",
                sku="HKT-73",
                serial_no=f"SN-8821{i}",
                cert_ids=["cert-7732"],
                qty=1.0,
            )
        )
    return reg


def main() -> None:
    print("=" * 74)
    print("Axis-Oilfield — MTR chain-of-custody demo (Axis Core signing)")
    print("=" * 74)

    reg = setup_registry()
    keeper = Storekeeper("K-12")
    reg.facts["verify"] = keeper.verify  # gateway verifies signatures

    def ev(event_type, item_id, sku, to_ref="", expected_sku="", work_order=True):
        event = InventoryEvent(
            event_type=event_type,
            item_id=item_id,
            sku=sku,
            from_ref="",
            to_ref=to_ref,
            actor=keeper.public_key_b64,
            certificates=["cert-7732"],  # HKT* requires a quality certificate
        )
        keeper.sign(event)
        outcome = reg.apply(event, expected_sku=expected_sku, has_work_order=work_order)
        return outcome

    # 1. Signed receipt at the warehouse.
    for i in range(3):
        o = ev("receive", f"tube-{i + 1:02d}", "HKT-73", to_ref="WH-01:row-A:shelf-1:cell-1")
        assert o.accepted, o.reason
    print("1. Приёмка 3 × НКТ-73 на склад        ✅ подписано и принято")

    # 2. Move one unit to pad 12.
    o = ev("store", "tube-01", "HKT-73", to_ref="PAD-12:container-B:shelf-3:cell-7")
    assert o.accepted, o.reason
    print("2. Перемещение НКТ-73 на куст 12       ✅ custody: склад → куст 12")

    # 3. Пересортица: work order for НКТ-89, scanned item is НКТ-73 → denied.
    o = ev("issue", "tube-02", "HKT-73", to_ref="well-8", expected_sku="HKT-89")
    print(f"3. Выдача по наряду на НКТ-89          ⛔ {o.reason}")

    # 4. Move a second unit to pad 12 (so two units are there).
    o = ev("store", "tube-02", "HKT-73", to_ref="PAD-12:container-B:shelf-3:cell-7")
    assert o.accepted, o.reason

    # 5. Inventory check on pad 12: expected 2, scanned 1 → discrepancy.
    report = reg.inventory_check(
        "PAD-12:container-B:shelf-3:cell-7", {"HKT-73": 1.0}
    )
    print(f"5. Инвентаризация куста 12             ⚠ расхождение: {report}")



    # 6. Custody break: someone claims tube-01 is on pad 5, last event says pad 12.
    breaks = reg.detect_custody_breaks({"tube-01": "PAD-5:container-B:pallet-2"})
    print(f"6. «Труба на кусте 5»?                 ⚠ custody break: {breaks}")

    # 7. Offline signing: signed at the pad without network, synced later.
    print("7. Оффлайн-подпись на кусте            🔒 событие подписано локально, сеть не нужна")
    offline = InventoryEvent(
        event_type="store", item_id="tube-03", sku="HKT-73",
        to_ref="PAD-12:container-B:shelf-3:cell-7", actor=keeper.public_key_b64,
        certificates=["cert-7732"],
    )
    keeper.sign(offline)  # signing requires no Internet
    o = reg.apply(offline)  # sync happens when back on the network
    assert o.accepted, o.reason
    print("   …синхронизировано при возврате на базу ✅")

    # 8. «Куст map»: where is HKT-73 now?
    print("\nКарта «что где лежит» (НКТ-73):")
    for row in reg.find_by_sku("HKT-73"):
        print(
            f"  {row['sku']} @ {row['location']:<38} "
            f"({row['last_event']} {row['last_event_actor'][:8]}…)"
        )

    print("\nВсе события подписаны Ed25519 и проверены ядром Axis Core.")


if __name__ == "__main__":
    main()
