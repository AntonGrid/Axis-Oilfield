"""The inventory registry — the «куст map».

Pure domain logic: applies signed events, tracks where each item is,
answers «where is SKU X?», detects custody breaks and produces inventory
discrepancy reports. Signature *verification* is done by the caller via
Axis Core; this registry only consumes already-validated events.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from oilfield.model import InventoryEvent, Item, Location, Site
from oilfield.policies import check_event


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


@dataclass
class EventOutcome:
    """Result of applying one event."""

    accepted: bool
    reason: str
    event: Optional[InventoryEvent] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"accepted": self.accepted, "reason": self.reason}


class InventoryRegistry:
    """Owns the site graph, the item registry and the append-only event log."""

    def __init__(self) -> None:
        self.sites: Dict[str, Site] = {}
        self.locations: Dict[str, Location] = {}
        self.items: Dict[str, Item] = {}
        self.events: List[InventoryEvent] = []
        #: nonce -> True (idempotent sync / replay guard).
        self._nonces: Dict[str, bool] = {}
        #: certificates and lot expiry are registry-level facts used by policies.
        self.certificates: Dict[str, Any] = {}
        self.lot_expiry: Dict[str, str] = {}
        #: signed inventory snapshots (observations of reality).
        self.snapshots: List[Any] = []
        self._snapshot_nonces: Dict[str, bool] = {}
        #: policy overrides / runtime facts (certificates, work orders, expiry).
        self.facts: Dict[str, Any] = {}

    # ── graph setup ──────────────────────────────────────────────────────────

    def add_site(self, site_id: str, name: str = "", region: str = "") -> Site:
        site = Site(site_id, name, region)
        self.sites[site_id] = site
        return site

    def add_location(self, location_id: str, site_id: str, kind: str = "area") -> Location:
        loc = Location(location_id, site_id, kind=kind)
        self.locations[loc.address] = loc
        return loc

    def add_item(self, item: Item) -> Item:
        self.items[item.item_id] = item
        return item

    def known_locations(self) -> set:
        return set(self.locations)

    # ── applying events ──────────────────────────────────────────────────────

    def apply(
        self,
        event: InventoryEvent,
        *,
        verify_signature: bool = True,
        expected_sku: str = "",
        has_work_order: bool = True,
        facts: Optional[Dict[str, Any]] = None,
    ) -> EventOutcome:
        """Run policies, bind the item to its location, append the event.

        Signature verification is delegated to ``self.facts['verify']`` when
        provided by the caller (the trust layer); the domain never checks
        crypto by itself.
        """
        nonce_key = f"{event.actor}:{event.nonce}"
        if nonce_key in self._nonces:
            return EventOutcome(False, "replay: nonce already seen")

        if verify_signature and "verify" in self.facts:
            ok, reason = self.facts["verify"](event)
            if not ok:
                return EventOutcome(False, reason)

        cert_ids = (facts or {}).get("cert_ids", event.certificates)
        item_expiry = (facts or {}).get("item_expiry", {})
        candidate = (facts or {}).get("candidate_lot", "")
        ok, reason = check_event(
            event.to_payload()["payload"],
            expected_sku=expected_sku,
            known_locations=self.known_locations(),
            cert_ids=cert_ids,
            has_work_order=has_work_order,
            item_expiry=item_expiry,
            candidate=candidate,
            temp_c=(facts or {}).get("temp_c"),
        )
        if not ok:
            return EventOutcome(False, reason)

        # Issue guards: the unit must be in stock and not already issued
        # (double issue = одна и та же труба выдана дважды).
        if event.event_type == "issue":
            item = self.items.get(event.item_id)
            if item is None or not item.location:
                return EventOutcome(False, "item_not_in_stock")
            if item.is_issued:
                return EventOutcome(False, "double_issue: already issued")

        # Bind the item to the target location (receive/store/return).
        if event.event_type in ("receive", "store", "return"):
            item = self.items.get(event.item_id)
            if item is None:
                item = self.add_item(
                    Item(item_id=event.item_id, sku=event.sku, qty=event.qty)
                )
            elif event.sku and item.sku and event.sku != item.sku:
                # Пересортица: the event names a different SKU than the unit has.
                return EventOutcome(
                    False,
                    f"sku_mismatch_on_item: event {event.sku!r} != item {item.sku!r}",
                )
            item.sku = event.sku or item.sku
            item.location = event.to_ref or item.location
            if event.certificates:
                item.cert_ids = list(dict.fromkeys(item.cert_ids + event.certificates))

        # Issue marks the item as moved out (location stays for reporting).
        if event.event_type == "issue":
            item = self.items.get(event.item_id)
            if item is not None:
                item.location = f"issued:{event.to_ref}"

        self._nonces[nonce_key] = True
        self.events.append(event)
        return EventOutcome(True, "ok", event=event)



    # ── queries: the «куст map» ──────────────────────────────────────────────

    def find_by_sku(self, sku: str) -> List[Dict[str, Any]]:
        """«Where is SKU X?» — per item, the current location and last event."""
        out = []
        for item in self.items.values():
            if item.sku != sku:
                continue
            last = self._last_event(item.item_id)
            out.append(
                {
                    "item_id": item.item_id,
                    "sku": item.sku,
                    "location": item.location or "unknown",
                    "qty": item.qty,
                    "last_event": last.event_type if last else None,
                    "last_event_ts": last.ts if last else None,
                    "last_event_actor": last.actor if last else None,
                }
            )
        return out

    def item_location(self, item_id: str) -> Optional[str]:
        item = self.items.get(item_id)
        return item.location if item else None

    def _last_event(self, item_id: str) -> Optional[InventoryEvent]:
        for event in reversed(self.events):
            if event.item_id == item_id:
                return event
        return None

    # ── anomalies ────────────────────────────────────────────────────────────

    def detect_custody_breaks(self, claims: Dict[str, str]) -> List[Dict[str, Any]]:
        """Flag items whose *claimed* location ≠ their last event's ``to``.

        ``claims``: ``{item_id: claimed_location}`` (e.g. «said to be on pad 5»).
        """
        breaks = []
        for item_id, claimed in claims.items():
            last = self._last_event(item_id)
            if last is None:
                continue
            actual = last.to_ref
            if actual and claimed and actual != claimed:
                breaks.append(
                    {
                        "item_id": item_id,
                        "sku": self.items[item_id].sku if item_id in self.items else "?",
                        "claimed": claimed,
                        "actual": actual,
                        "anomaly": "custody_break",
                    }
                )
        return breaks

    def inventory_check(
        self, location_id: str, actual: Dict[str, float]
    ) -> List[Dict[str, Any]]:
        """Compare observed reality at a location with the registry expectation.

        ``actual``: ``{sku: qty}`` observed by scanning. Returns the list of
        discrepancies (пересортица report) — over/under per SKU.
        """
        expected: Dict[str, float] = {}
        for item in self.items.values():
            if item.location == location_id:
                expected[item.sku] = expected.get(item.sku, 0.0) + item.qty

        skus = sorted(set(expected) | set(actual))
        report = []
        for sku in skus:
            exp = expected.get(sku, 0.0)
            act = actual.get(sku, 0.0)
            if exp != act:
                report.append(
                    {
                        "location": location_id,
                        "sku": sku,
                        "expected": exp,
                        "actual": act,
                        "diff": round(act - exp, 3),
                    }
                )
        return report

    # ── certificates & lots (policy facts) ───────────────────────────────────

    def add_certificate(self, certificate: Any) -> Any:
        self.certificates[certificate.cert_id] = certificate
        return certificate

    def set_lot_expiry(self, batch_no: str, expiry_iso: str) -> None:
        """Record a lot's expiry (used by FEFO on issue)."""
        self.lot_expiry[batch_no] = expiry_iso

    def expiring_lots(self, sku: str, before_iso: str) -> List[Dict[str, str]]:
        """Lots of a SKU that expire before the given date (ISO)."""
        out = []
        for item in self.items.values():
            if item.sku != sku or not item.batch_no:
                continue
            exp = self.lot_expiry.get(item.batch_no)
            if exp and exp < before_iso and not item.is_issued:
                out.append({"batch_no": item.batch_no, "expiry": exp})
        return out

    # ── signed inventory snapshots ───────────────────────────────────────────

    def record_snapshot(
        self,
        snapshot: Any,
        *,
        verify_signature: bool = True,
    ) -> EventOutcome:
        """Accept a signed inventory snapshot (observed reality).

        ``EventOutcome.event`` carries the snapshot object. Signature
        verification is delegated to ``self.facts['verify_snapshot']``.
        """
        nonce_key = f"snap:{snapshot.actor}:{snapshot.nonce}"
        if nonce_key in self._snapshot_nonces:
            return EventOutcome(False, "replay: snapshot nonce already seen")
        if verify_signature and "verify_snapshot" in self.facts:
            ok, reason = self.facts["verify_snapshot"](snapshot)
            if not ok:
                return EventOutcome(False, reason)
        self._snapshot_nonces[nonce_key] = True
        self.snapshots.append(snapshot)
        return EventOutcome(True, "ok", event=snapshot)

    def latest_snapshot(self, location_id: str) -> Optional[Any]:
        for snapshot in reversed(self.snapshots):
            if snapshot.location_id == location_id:
                return snapshot
        return None

    def inventory_report(
        self, location_id: str, snapshot: Optional[Any] = None
    ) -> List[Dict[str, Any]]:
        """Пересортица report for the last signed snapshot of a location.

        ``snapshot.observations`` is ``{item_id: {sku, qty, serial_no}}`` as
        actually scanned; expectations come from the registry itself.
        """
        snap = snapshot or self.latest_snapshot(location_id)
        if snap is None:
            return [{"location": location_id, "error": "no_snapshot"}]

        expected_by_sku: Dict[str, float] = {}
        expected_items: Dict[str, Any] = {}
        for item in self.items.values():
            if item.location == location_id:
                expected_by_sku[item.sku] = expected_by_sku.get(item.sku, 0.0) + item.qty
                expected_items[item.item_id] = item

        actual_by_sku: Dict[str, float] = {}
        actual_items: Dict[str, Any] = snap.observations
        for item_id, obs in actual_items.items():
            actual_by_sku[obs["sku"]] = actual_by_sku.get(obs["sku"], 0.0) + obs.get("qty", 1.0)

        skus = sorted(set(expected_by_sku) | set(actual_by_sku))
        report = []
        for sku in skus:
            exp = expected_by_sku.get(sku, 0.0)
            act = actual_by_sku.get(sku, 0.0)
            if exp != act:
                report.append(
                    {
                        "location": location_id,
                        "sku": sku,
                        "expected": exp,
                        "actual": act,
                        "diff": round(act - exp, 3),
                        "snapshot_id": snap.snapshot_id,
                    }
                )
        # Item-level: present in the ledger but missing on the shelf (потеряшка).
        for item_id, item in expected_items.items():
            if item_id not in actual_items and not item.is_issued:
                report.append(
                    {
                        "location": location_id,
                        "sku": item.sku,
                        "item_id": item_id,
                        "anomaly": "item_missing_on_shelf",
                    }
                )
        return report

    # ── anomaly detectors (signals, not decisions — constitution C-1) ───────

    def detect_lost_items(
        self, now_iso: Optional[str] = None, days: int = 30
    ) -> List[Dict[str, Any]]:
        """Items with no signed event for > ``days`` while still in storage."""
        now = dt.datetime.fromisoformat(now_iso) if now_iso else dt.datetime.now(dt.timezone.utc)
        lost = []
        for item in self.items.values():
            if not item.location or item.is_issued:
                continue
            last = self._last_event(item.item_id)
            if last is None or not last.ts:
                continue
            try:
                last_dt = dt.datetime.fromisoformat(last.ts)
            except ValueError:
                continue
            age_days = (now - last_dt).days
            if age_days > days:
                lost.append(
                    {
                        "item_id": item.item_id,
                        "sku": item.sku,
                        "location": item.location,
                        "last_event_ts": last.ts,
                        "days_since": age_days,
                        "anomaly": "lost_item",
                    }
                )
        return lost

    # ── queries for daily warehouse work ─────────────────────────────────────

    def history(self, item_id: str) -> List[Any]:
        """All signed events for an item, oldest → newest."""
        return [e for e in self.events if e.item_id == item_id]

    def location_contents(self, location_id: str) -> List[Dict[str, Any]]:
        """Everything stored at an address («что лежит в ячейке»)."""
        out = []
        for item in self.items.values():
            if item.location == location_id:
                out.append(
                    {
                        "item_id": item.item_id,
                        "sku": item.sku,
                        "serial_no": item.serial_no,
                        "batch_no": item.batch_no,
                        "qty": item.qty,
                        "cert_ids": item.cert_ids,
                        "status": item.status,
                    }
                )
        return out

    def search(self, term: str) -> List[Dict[str, Any]]:
        """Search by item id / serial / batch / sku (substring, case-insensitive)."""
        term = term.lower()
        out = []
        for item in self.items.values():
            hay = " ".join(
                str(x) for x in (item.item_id, item.sku, item.serial_no, item.batch_no) if x
            ).lower()
            if term in hay:
                last = self._last_event(item.item_id)
                out.append(
                    {
                        "item_id": item.item_id,
                        "sku": item.sku,
                        "serial_no": item.serial_no,
                        "batch_no": item.batch_no,
                        "location": item.location or "unknown",
                        "qty": item.qty,
                        "last_event": last.event_type if last else None,
                        "last_event_ts": last.ts if last else None,
                    }
                )
        return out

    def stock_summary(self) -> Dict[str, Any]:
        """«Карта кустов»: per-site counts of stored items and distinct SKUs."""
        summary: Dict[str, Any] = {}
        for item in self.items.values():
            if not item.location or item.is_issued:
                continue
            site = item.location.split(":")[0]
            node = summary.setdefault(site, {"items": 0, "skus": set()})
            node["items"] += 1
            node["skus"].add(item.sku)
        for site in summary:
            summary[site]["skus"] = sorted(summary[site]["skus"])
        return summary
