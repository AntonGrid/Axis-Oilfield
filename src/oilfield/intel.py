"""PoI (Proof-of-Intelligence) light layer for Axis-Oilfield.

Per the Axis constitution (C-1), everything here produces **signals, not
decisions**:
- ``site_accuracy`` — how truthful a site's custody is (measured from the
  signed inventory snapshots);
- ``demand_profile`` — per-site, per-SKU consumption estimated from signed
  ``issue`` events;
- ``SiteContribution`` — a **signed** federated artefact a site publishes
  (domain="oilfield", period, consumption, self-accuracy). The signature is
  Ed25519 over the canonical message — the same wire format as Axis Core;
- ``aggregate`` — reputation-weighted combination of site contributions;
- ``rebalance_suggestions`` — where a pad will run short and where the SKU
  is in surplus (a suggestion, never an order).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from oilfield.model import InventorySnapshot

#: how many days ahead the simple demand forecast looks.
HORIZON_DAYS = 14

DOMAIN = "oilfield"


def _site_of(location: str) -> str:
    return (location or "").split(":")[0]


def site_accuracy(reg, site_id: str) -> Optional[Dict[str, Any]]:
    """Custody truthfulness of a site from its latest signed snapshots.

    For every location of the site that has a snapshot we compare the
    observed reality with the registry expectation. ``accuracy`` = 1 −
    (mismatched units / expected units). ``None`` if the site has no
    snapshots yet (no evidence → neutral, not punished).
    """
    locs = [l for l in reg.locations if l.startswith(site_id + ":")]
    mismatched = 0
    total = 0
    checked = 0
    for loc in locs:
        snap = reg.latest_snapshot(loc)
        if snap is None:
            continue
        checked += 1
        # expected units at this location (non-issued items bound here)
        exp: Dict[str, float] = {}
        for item in reg.items.values():
            if item.location == loc and not item.is_issued:
                exp[item.sku] = exp.get(item.sku, 0.0) + item.qty
        act: Dict[str, float] = {}
        for obs in snap.observations.values():
            act[obs["sku"]] = act.get(obs["sku"], 0.0) + obs.get("qty", 1.0)
        for sku in set(exp) | set(act):
            e = exp.get(sku, 0.0)
            a = act.get(sku, 0.0)
            total += max(e, a)
            mismatched += abs(a - e)
    if checked == 0:
        return None
    accuracy = round(1.0 - (mismatched / total if total else 0.0), 4)
    return {"site_id": site_id, "locations_checked": checked,
            "accuracy": accuracy, "units_checked": total}


def _last_location_before(reg, item_id: str, before_event) -> str:
    """Where the item was bound before the given event (from the log)."""
    location = ""
    for e in reg.events:
        if e is before_event:
            break
        if e.item_id != item_id:
            continue
        if e.event_type in ("receive", "store", "return"):
            location = e.to_ref
        elif e.event_type == "issue":
            location = ""
    return location


def demand_profile(reg, sku: str, days: int = 30) -> Dict[str, Dict[str, Any]]:
    """Per-site issued quantity of a SKU over the last ``days``.

    Consumption is attributed to the site the item was bound to at the moment
    of the issue (the «куст» that actually used it).
    """
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    per_site: Dict[str, float] = {}
    for e in reg.events:
        if e.event_type != "issue" or e.sku != sku:
            continue
        try:
            ts = dt.datetime.fromisoformat(e.ts)
        except (ValueError, TypeError):
            continue
        if ts < since:
            continue
        loc = _last_location_before(reg, e.item_id, e)
        site = _site_of(loc)
        per_site[site] = per_site.get(site, 0.0) + e.qty
    profile = {}
    for site, qty in per_site.items():
        daily = qty / max(days, 1)
        profile[site] = {
            "issued": qty,
            "daily_avg": round(daily, 3),
            "horizon_need": round(daily * HORIZON_DAYS, 3),
        }
    return profile


def available_at_site(reg, site_id: str, sku: str) -> float:
    return sum(
        i.qty for i in reg.items.values()
        if i.sku == sku and i.location and not i.is_issued
        and _site_of(i.location) == site_id
    )


@dataclass
class SiteContribution:
    """A signed federated contribution of one site (PoI artefact)."""

    site_id: str
    actor: str
    period_from: str
    period_to: str
    consumption: Dict[str, float] = field(default_factory=dict)  # sku -> qty
    accuracy: Optional[float] = None
    nonce: int = 0
    timestamp: str = ""
    signature: str = ""

    def to_payload(self) -> Dict[str, Any]:
        return {
            "algo": "ed25519",
            "device_id": self.actor,
            "nonce": str(self.nonce),
            "timestamp": self.timestamp,
            "payload": {
                "domain": DOMAIN,
                "kind": "contribution",
                "site_id": self.site_id,
                "period_from": self.period_from,
                "period_to": self.period_to,
                "accuracy": self.accuracy,
                "consumption": {k: v for k, v in sorted(self.consumption.items())},
            },
        }

    def to_dict(self) -> Dict[str, Any]:
        d = self.to_payload()
        d["payload"]["signature"] = self.signature
        return d

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "SiteContribution":
        pl = raw.get("payload", raw)
        return cls(
            site_id=pl.get("site_id", ""),
            actor=raw.get("device_id", ""),
            period_from=pl.get("period_from", ""),
            period_to=pl.get("period_to", ""),
            consumption=dict(pl.get("consumption", {})),
            accuracy=pl.get("accuracy"),
            nonce=int(raw.get("nonce", 0) or 0),
            timestamp=raw.get("timestamp", ""),
            signature=pl.get("signature", ""),
        )


def build_contribution(
    reg,
    *,
    site_id: str,
    actor: str,
    key,
    skus: Optional[List[str]] = None,
    days: int = 30,
) -> SiteContribution:
    """Build and sign a site contribution (issued consumption + accuracy)."""
    from datetime import timedelta
    from axis_core.signature_utils import sign_proof

    now = dt.datetime.now(dt.timezone.utc)
    sku_list = skus or sorted({i.sku for i in reg.items.values()})
    consumption = {}
    for sku in sku_list:
        prof = demand_profile(reg, sku, days=days)
        if site_id in prof:
            consumption[sku] = prof[site_id]["issued"]
    acc = site_accuracy(reg, site_id)
    c = SiteContribution(
        site_id=site_id,
        actor=actor,
        period_from=(now - timedelta(days=days)).isoformat(timespec="seconds"),
        period_to=now.isoformat(timespec="seconds"),
        consumption=consumption,
        accuracy=acc["accuracy"] if acc else None,
    )
    # nonce: last event nonce of this actor in the registry, +1
    c.nonce = max([e.nonce for e in reg.events if e.actor == actor] or [0]) + 1
    c.timestamp = now.isoformat(timespec="seconds")
    c.signature = sign_proof(key, c.to_payload())
    return c


def aggregate(contributions: List[SiteContribution]) -> Dict[str, Any]:
    """Reputation-weighted aggregate demand per SKU across sites.

    Weight = contribution accuracy (default 0.5 when unknown). Output is a
    signal for logistics: expected total need per SKU in the horizon.
    """
    by_sku: Dict[str, Dict[str, float]] = {}
    for c in contributions:
        w = 0.5 if c.accuracy is None else c.accuracy
        for sku, qty in c.consumption.items():
            node = by_sku.setdefault(sku, {"sum_w": 0.0, "sum_wq": 0.0, "sites": {}})
            node["sum_w"] += w
            node["sum_wq"] += w * qty
            node["sites"][c.site_id] = round(qty, 3)
    out = {}
    for sku, node in sorted(by_sku.items()):
        if node["sum_w"] > 0:
            out[sku] = {
                "weighted_period_qty": round(node["sum_wq"] / node["sum_w"], 3),
                "horizon_need": round(node["sum_wq"] / node["sum_w"] * HORIZON_DAYS / 30.0, 3),
                "sites": node["sites"],
            }
    return out


def rebalance_suggestions(
    reg, contributions: List[SiteContribution]
) -> List[Dict[str, Any]]:
    """Suggest moving SKUs from surplus sites to pads running short.

    A suggestion only — the Policy Engine / human decides (C-1).
    """
    agg = aggregate(contributions)
    suggestions = []
    for sku, info in sorted(agg.items()):
        for site in sorted(info["sites"]):
            horizon_need = info["sites"][site]
            available = available_at_site(reg, site, sku)
            if available < horizon_need:
                deficit = round(horizon_need - available, 3)
                suggestions.append(
                    {
                        "sku": sku,
                        "site": site,
                        "available": available,
                        "forecast_need": horizon_need,
                        "deficit": deficit,
                        "anomaly": "shortage_forecast",
                    }
                )
    return suggestions
