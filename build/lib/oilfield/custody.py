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
        )
        if not ok:
            return EventOutcome(False, reason)

        # Bind the item to the target location (receive/store/return).
        if event.event_type in ("receive", "store", "return"):
            item = self.items.get(event.item_id)
            if item is None:
                item = self.add_item(
                    Item(item_id=event.item_id, sku=event.sku, qty=event.qty)
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
