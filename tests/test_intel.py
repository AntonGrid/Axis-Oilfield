"""PoI layer tests (src/oilfield/intel)."""

import datetime as dt
import base64

import nacl.signing

from oilfield import InventoryRegistry, InventorySnapshot
from oilfield.model import InventoryEvent, Item
from oilfield.intel import (
    HORIZON_DAYS,
    SiteContribution,
    aggregate,
    build_contribution,
    demand_profile,
    rebalance_suggestions,
    site_accuracy,
)
from axis_core.signature_utils import (
    encode_public_key,
    verify_ed25519_signature,
)


def _reg():
    reg = InventoryRegistry()
    reg.add_site("WH-01", "Warehouse")
    reg.add_site("PAD-12", "Pad 12")
    reg.add_location("WH-01:яч-1", "WH-01", "cell")
    reg.add_location("PAD-12:яч-7", "PAD-12", "cell")
    return reg


def _ev(event_type="store", item_id="tube-01", sku="HKT-73", to_ref="PAD-12:яч-7",
        nonce=1, actor="a1", qty=1.0):
    return InventoryEvent(
        event_type=event_type, item_id=item_id, sku=sku, to_ref=to_ref,
        actor=actor, nonce=nonce, certificates=["c1"], qty=qty,
    )


def test_site_accuracy_perfect_and_mismatch():
    reg = _reg()
    reg.add_item(Item(item_id="t1", sku="HKT-73"))
    reg.add_item(Item(item_id="t2", sku="HKT-73"))
    reg.apply(_ev("receive", item_id="t1", to_ref="WH-01:яч-1", nonce=1))
    reg.apply(_ev("receive", item_id="t2", to_ref="WH-01:яч-1", nonce=2))

    # perfect snapshot → accuracy 1.0
    reg.record_snapshot(InventorySnapshot(
        snapshot_id="s1", location_id="WH-01:яч-1", actor="a1", nonce=1,
        observations={"t1": {"sku": "HKT-73", "qty": 1.0},
                      "t2": {"sku": "HKT-73", "qty": 1.0}}))
    acc = site_accuracy(reg, "WH-01")
    assert acc is not None and acc["accuracy"] == 1.0

    # a wrong snapshot (only one tube) lowers accuracy
    reg.record_snapshot(InventorySnapshot(
        snapshot_id="s2", location_id="WH-01:яч-1", actor="a1", nonce=2,
        observations={"t1": {"sku": "HKT-73", "qty": 1.0}}))
    acc = site_accuracy(reg, "WH-01")
    assert 0.0 < acc["accuracy"] < 1.0


def test_site_accuracy_unknown_without_snapshots():
    reg = _reg()
    assert site_accuracy(reg, "PAD-12") is None


def _ts(days_ago: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)
            ).isoformat(timespec="seconds")


def test_demand_profile_counts_issues_per_site():
    reg = _reg()
    reg.add_item(Item(item_id="t1", sku="HKT-73"))
    reg.add_item(Item(item_id="t2", sku="HKT-73"))
    reg.apply(_ev("receive", item_id="t1", to_ref="PAD-12:яч-7", nonce=1))
    reg.apply(_ev("receive", item_id="t2", to_ref="PAD-12:яч-7", nonce=2))
    for n, item in enumerate(["t1", "t2"]):
        ev = _ev("issue", item_id=item, to_ref="well-8", nonce=3 + n)
        ev.ts = _ts(5)
        reg.apply(ev)
    prof = demand_profile(reg, "HKT-73", days=30)
    assert prof["PAD-12"]["issued"] == 2.0
    assert prof["PAD-12"]["horizon_need"] > 0


def test_contribution_signed_and_aggregate_weights():
    reg = _reg()
    reg.add_item(Item(item_id="t1", sku="HKT-73"))
    reg.apply(_ev("receive", item_id="t1", to_ref="PAD-12:яч-7", nonce=1))
    ev = _ev("issue", item_id="t1", to_ref="well-9", nonce=2)
    ev.ts = _ts(3)
    reg.apply(ev)

    key = nacl.signing.SigningKey.generate()
    actor = encode_public_key(key)
    c = build_contribution(reg, site_id="PAD-12", actor=actor, key=key, days=30)
    assert c.consumption.get("HKT-73", 0) == 1.0

    # signature must verify over the canonical payload
    from axis_core.signature_utils import canonical_proof_message
    ok = verify_ed25519_signature(
        actor, canonical_proof_message(c.to_payload()), c.signature)
    assert ok

    # from_dict round-trip keeps the signature verifiable
    c2 = SiteContribution.from_dict(c.to_dict())
    assert c2.signature == c.signature
    assert verify_ed25519_signature(
        actor, canonical_proof_message(c2.to_payload()), c2.signature)


def test_rebalance_suggests_shortage():
    reg = _reg()
    reg.add_item(Item(item_id="t1", sku="HKT-73"))
    reg.apply(_ev("receive", item_id="t1", to_ref="PAD-12:яч-7", nonce=1))
    ev = _ev("issue", item_id="t1", to_ref="well-9", nonce=2)
    ev.ts = _ts(2)
    reg.apply(ev)
    # PAD-12 consumed a lot and now has 0 in stock
    key = nacl.signing.SigningKey.generate()
    actor = encode_public_key(key)
    c = build_contribution(reg, site_id="PAD-12", actor=actor, key=key, days=30)
    sugg = rebalance_suggestions(reg, [c])
    assert any(s["site"] == "PAD-12" and s["deficit"] > 0 for s in sugg)
