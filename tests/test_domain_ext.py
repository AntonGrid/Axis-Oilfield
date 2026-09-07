"""Extended domain tests: issue guards, temperature, snapshots, lost items,
search & the offline outbox (src/oilfield)."""

from oilfield import InventoryRegistry, InventorySnapshot
from oilfield.model import InventoryEvent, Item
from oilfield.sync import EventOutbox


def _reg():
    reg = InventoryRegistry()
    reg.add_site("WH-01", "Warehouse")
    reg.add_site("PAD-12", "Pad 12")
    reg.add_location("WH-01:cell-1", "WH-01", "cell")
    reg.add_location("PAD-12:cell-7", "PAD-12", "cell")
    return reg


def _ev(event_type="store", item_id="tube-01", sku="HKT-73", to_ref="PAD-12:cell-7",
        nonce=1, actor="actor-1"):
    return InventoryEvent(
        event_type=event_type, item_id=item_id, sku=sku, to_ref=to_ref,
        actor=actor, nonce=nonce, certificates=["cert-1"],
    )


# ── issue guards ─────────────────────────────────────────────────────────────

def test_issue_requires_item_in_stock():
    reg = _reg()
    o = reg.apply(_ev("issue", to_ref="well-8"))
    assert not o.accepted
    assert "item_not_in_stock" in o.reason


def test_double_issue_rejected():
    reg = _reg()
    reg.add_item(Item(item_id="tube-01", sku="HKT-73"))
    assert reg.apply(_ev("receive", to_ref="WH-01:cell-1", nonce=1)).accepted
    assert reg.apply(_ev("issue", to_ref="well-8", nonce=2)).accepted
    o = reg.apply(_ev("issue", to_ref="well-9", nonce=3))
    assert not o.accepted
    assert "double_issue" in o.reason


def test_return_brings_item_back_to_storage():
    reg = _reg()
    reg.add_item(Item(item_id="tube-01", sku="HKT-73"))
    assert reg.apply(_ev("receive", to_ref="WH-01:cell-1", nonce=1)).accepted
    assert reg.apply(_ev("issue", to_ref="well-8", nonce=2)).accepted
    assert reg.apply(_ev("return", to_ref="WH-01:cell-1", nonce=3)).accepted
    assert reg.item_location("tube-01") == "WH-01:cell-1"


# ── temperature for chemicals ────────────────────────────────────────────────

def test_reagent_out_of_band_rejected():
    reg = _reg()
    reg.add_location("WH-01:cool-1", "WH-01", "cell")
    reg.add_item(Item(item_id="reag-1", sku="REAGENT-X", qty=1.0))
    o = reg.apply(
        _ev("receive", item_id="reag-1", sku="REAGENT-X", to_ref="WH-01:cool-1", nonce=1),
        facts={"temp_c": -18.0},
    )
    assert not o.accepted
    assert "temperature_out_of_range" in o.reason


def test_reagent_in_band_accepted():
    reg = _reg()
    reg.add_location("WH-01:cool-1", "WH-01", "cell")
    reg.add_item(Item(item_id="reag-1", sku="REAGENT-X", qty=1.0))
    o = reg.apply(
        _ev("receive", item_id="reag-1", sku="REAGENT-X", to_ref="WH-01:cool-1", nonce=1),
        facts={"temp_c": 5.0},
    )
    assert o.accepted


# ── lots / expiry ────────────────────────────────────────────────────────────

def test_expiring_lots():
    reg = _reg()
    reg.add_item(Item(item_id="t1", sku="HKT-73", batch_no="B1"))
    reg.add_item(Item(item_id="t2", sku="HKT-73", batch_no="B2"))
    reg.set_lot_expiry("B1", "2026-01-01")
    reg.set_lot_expiry("B2", "2027-01-01")
    lots = reg.expiring_lots("HKT-73", "2026-06-01")
    assert [l["batch_no"] for l in lots] == ["B1"]


# ── signed snapshots & inventory report ──────────────────────────────────────

def test_snapshot_replay_guard():
    reg = _reg()
    snap = InventorySnapshot(
        snapshot_id="s1", location_id="WH-01:cell-1", actor="actor-1",
        nonce=1, observations={"tube-01": {"sku": "HKT-73", "qty": 1.0}},
    )
    assert reg.record_snapshot(snap).accepted
    assert not reg.record_snapshot(snap).accepted


def test_inventory_report_missing_item_and_count():
    reg = _reg()
    reg.add_item(Item(item_id="tube-01", sku="HKT-73"))
    reg.add_item(Item(item_id="tube-02", sku="HKT-73"))
    reg.apply(_ev("receive", item_id="tube-01", to_ref="WH-01:cell-1", nonce=1))
    reg.apply(_ev("receive", item_id="tube-02", to_ref="WH-01:cell-1", nonce=2))
    snap = InventorySnapshot(
        snapshot_id="s9", location_id="WH-01:cell-1", actor="actor-1", nonce=9,
        observations={"tube-01": {"sku": "HKT-73", "qty": 1.0}},
    )
    assert reg.record_snapshot(snap).accepted
    report = reg.inventory_report("WH-01:cell-1")
    assert any(r.get("anomaly") == "item_missing_on_shelf" for r in report)
    assert any("diff" in r for r in report)


def test_inventory_report_without_snapshot():
    reg = _reg()
    assert reg.inventory_report("WH-01:cell-1")[0]["error"] == "no_snapshot"


# ── lost items ───────────────────────────────────────────────────────────────

def test_lost_items_detected_after_threshold():
    reg = _reg()
    reg.add_item(Item(item_id="tube-01", sku="HKT-73"))
    ev = _ev("receive", to_ref="WH-01:cell-1", nonce=1)
    ev.ts = "2026-01-01T00:00:00+00:00"
    assert reg.apply(ev).accepted
    lost = reg.detect_lost_items(now_iso="2026-03-01T00:00:00+00:00", days=30)
    assert len(lost) == 1
    assert lost[0]["anomaly"] == "lost_item"


# ── daily queries ────────────────────────────────────────────────────────────

def test_history_and_location_contents():
    reg = _reg()
    reg.add_item(Item(item_id="tube-01", sku="HKT-73", serial_no="SN-1", batch_no="B1"))
    reg.apply(_ev("receive", to_ref="WH-01:cell-1", nonce=1))
    assert len(reg.history("tube-01")) == 1
    contents = reg.location_contents("WH-01:cell-1")
    assert len(contents) == 1 and contents[0]["item_id"] == "tube-01"


def test_search_by_serial_and_batch():
    reg = _reg()
    reg.add_item(Item(item_id="tube-01", sku="HKT-73", serial_no="SN-88213", batch_no="B7"))
    assert len(reg.search("SN-88213")) == 1
    assert len(reg.search("b7")) == 1


def test_stock_summary_groups_by_site():
    reg = _reg()
    reg.add_item(Item(item_id="t1", sku="HKT-73"))
    reg.add_item(Item(item_id="t2", sku="UKT-60"))
    reg.apply(_ev("receive", item_id="t1", sku="HKT-73", to_ref="WH-01:cell-1", nonce=1))
    reg.apply(_ev("receive", item_id="t2", sku="UKT-60", to_ref="WH-01:cell-1", nonce=2))
    reg.apply(_ev("store", item_id="t2", sku="UKT-60", to_ref="PAD-12:cell-7", nonce=3))
    s = reg.stock_summary()
    assert s["WH-01"]["items"] == 1
    assert s["PAD-12"]["skus"] == ["UKT-60"]


# ── offline outbox ───────────────────────────────────────────────────────────

def test_outbox_sync_applies_and_skips_replay(tmp_path):
    reg = _reg()
    reg.add_item(Item(item_id="tube-01", sku="HKT-73"))
    box = EventOutbox("pad-scanner")
    box.add(_ev("receive", to_ref="WH-01:cell-1", nonce=1))
    box.add(_ev("receive", to_ref="WH-01:cell-1", nonce=1))  # duplicate
    assert len(box) == 2

    res = box.sync(reg)
    assert res.synced == 1
    assert res.skipped == 1
    assert reg.item_location("tube-01") == "WH-01:cell-1"
    assert len(box) == 0


def test_outbox_keeps_rejected_and_persists(tmp_path):
    reg = _reg()
    reg.add_item(Item(item_id="tube-01", sku="HKT-73"))
    box = EventOutbox("pad-scanner")
    box.add(_ev("receive", to_ref="nowhere", nonce=1))  # unknown location

    path = str(tmp_path / "outbox.json")
    box.save(path)
    box2 = EventOutbox.load(path)
    assert len(box2) == 1

    res = box2.sync(reg)
    assert res.rejected == 1
    assert "unknown_location" in res.reasons[0]
    assert len(box2) == 1  # rejected stays for inspection

