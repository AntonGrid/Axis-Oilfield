"""Registry / custody tests (src/oilfield/custody)."""

from oilfield import InventoryRegistry
from oilfield.model import InventoryEvent, Item


def _registry():
    reg = InventoryRegistry()
    reg.add_site("WH-01", "Warehouse")
    reg.add_site("PAD-12", "Pad 12")
    reg.add_location("WH-01:cell-1", "WH-01", "cell")
    reg.add_location("PAD-12:cell-7", "PAD-12", "cell")
    reg.add_item(Item(item_id="tube-01", sku="HKT-73", qty=1.0))
    return reg


def _event(event_type="store", item_id="tube-01", sku="HKT-73", to_ref="PAD-12:cell-7", nonce=1):
    return InventoryEvent(
        event_type=event_type,
        item_id=item_id,
        sku=sku,
        to_ref=to_ref,
        actor="actor-1",
        nonce=nonce,
        certificates=["cert-1"],  # HKT* requires a quality certificate
    )


def test_receive_binds_item_to_location():
    reg = _registry()
    outcome = reg.apply(_event("receive", to_ref="WH-01:cell-1", nonce=1))
    assert outcome.accepted
    assert reg.item_location("tube-01") == "WH-01:cell-1"


def test_store_moves_item():
    reg = _registry()
    reg.apply(_event("receive", to_ref="WH-01:cell-1", nonce=1))
    reg.apply(_event("store", to_ref="PAD-12:cell-7", nonce=2))
    assert reg.item_location("tube-01") == "PAD-12:cell-7"


def test_peresortica_blocked():
    """A work order for НКТ-89 + scanned НКТ-73 → denied (sku_mismatch)."""
    reg = _registry()
    outcome = reg.apply(
        _event("issue", sku="HKT-73", to_ref="well-8", nonce=1),
        expected_sku="HKT-89",
        has_work_order=True,
    )
    assert not outcome.accepted
    assert "sku_mismatch" in outcome.reason


def test_issue_requires_work_order():
    reg = _registry()
    reg.apply(_event("receive", to_ref="WH-01:cell-1", nonce=1))
    outcome = reg.apply(
        _event("issue", to_ref="well-8", nonce=2),
        has_work_order=False,
    )
    assert not outcome.accepted
    assert "issue_not_authorized" in outcome.reason


def test_unknown_location_rejected():
    reg = _registry()
    outcome = reg.apply(_event("store", to_ref="nowhere", nonce=1))
    assert not outcome.accepted
    assert "unknown_location" in outcome.reason


def test_replay_rejected_by_nonce():
    reg = _registry()
    assert reg.apply(_event(nonce=1)).accepted
    outcome = reg.apply(_event(nonce=1))
    assert not outcome.accepted
    assert "replay" in outcome.reason


def test_custody_break_detection():
    reg = _registry()
    reg.apply(_event("store", to_ref="PAD-12:cell-7", nonce=1))
    breaks = reg.detect_custody_breaks({"tube-01": "PAD-5:pallet-2"})
    assert len(breaks) == 1
    assert breaks[0]["anomaly"] == "custody_break"
    assert breaks[0]["claimed"] == "PAD-5:pallet-2"
    assert breaks[0]["actual"] == "PAD-12:cell-7"


def test_inventory_check_reports_discrepancy():
    reg = _registry()
    reg.apply(_event("receive", to_ref="WH-01:cell-1", nonce=1))
    reg.add_item(Item(item_id="tube-02", sku="HKT-73", qty=1.0))
    reg.apply(_event("receive", item_id="tube-02", to_ref="WH-01:cell-1", nonce=2))

    report = reg.inventory_check("WH-01:cell-1", {"HKT-73": 1.0})
    assert len(report) == 1
    assert report[0]["sku"] == "HKT-73"
    assert report[0]["expected"] == 2.0
    assert report[0]["actual"] == 1.0
    assert report[0]["diff"] == -1.0


def test_find_by_sku_returns_kust_map():
    reg = _registry()
    reg.apply(_event("store", to_ref="PAD-12:cell-7", nonce=1))
    rows = reg.find_by_sku("HKT-73")
    assert len(rows) == 1
    assert rows[0]["location"] == "PAD-12:cell-7"
