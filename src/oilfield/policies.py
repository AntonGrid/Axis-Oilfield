"""The warehouse rulebook — pure policies evaluated at event time.

Every policy returns ``(ok: bool, reason: str)``. A violation rejects the
event **before** it is accepted into the registry.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

#: Temperature band for chemicals (cold-chain style, °C).
CHEMICAL_TEMP_MIN = 2.0
CHEMICAL_TEMP_MAX = 25.0

#: SKUs that always require a quality certificate.
CERTIFICATE_REQUIRED_SKUS = {"HKT", "UKT", "REAGENT", "UECN"}

#: SKUs sensitive to temperature (chemicals). When a ``temp_c`` fact is
#: supplied, storing/moving them outside the band is rejected.
TEMPERATURE_SENSITIVE_SKUS = {"REAGENT", "UECN"}


def sku_matches(scanned_sku: str, expected_sku: str) -> Tuple[bool, str]:
    """Пересортица gate: the scanned SKU must equal the document's SKU."""
    if scanned_sku != expected_sku:
        return False, f"sku_mismatch: scanned {scanned_sku!r} != expected {expected_sku!r}"
    return True, "ok"


def certificate_required(sku: str, cert_ids: list) -> Tuple[bool, str]:
    """SKUs from the certificate list must carry a valid certificate."""
    for prefix in CERTIFICATE_REQUIRED_SKUS:
        if sku.upper().startswith(prefix) and not cert_ids:
            return False, f"certificate_missing for {sku}"
    return True, "ok"


def fefo_issue(item_expiry: Dict[str, str], candidate: str) -> Tuple[bool, str]:
    """First-Expired-First-Out within the same SKU's lots.

    ``item_expiry``: ``{lot_id: expiry_date}`` of the candidate SKU.
    A lot may not be issued while an earlier-expiring lot of the same SKU is
    still present.
    """
    if candidate not in item_expiry:
        return True, "ok"
    candidate_exp = item_expiry[candidate]
    for lot, expiry in item_expiry.items():
        if lot != candidate and expiry < candidate_exp:
            return False, f"fefo_violation: lot {lot!r} expires earlier"
    return True, "ok"


def temperature_in_range(temp_c: float) -> Tuple[bool, str]:
    """Temperature-sensitive storage check."""
    if not (CHEMICAL_TEMP_MIN <= temp_c <= CHEMICAL_TEMP_MAX):
        return (
            False,
            f"temperature_out_of_range: {temp_c}°C "
            f"(band {CHEMICAL_TEMP_MIN}..{CHEMICAL_TEMP_MAX}°C)",
        )
    return True, "ok"


def issue_authorized(has_work_order: bool) -> Tuple[bool, str]:
    """Issue requires an authorized work order (наряд)."""
    if not has_work_order:
        return False, "issue_not_authorized"
    return True, "ok"


def location_known(location_id: str, known: set) -> Tuple[bool, str]:
    """The target address must exist in the site graph."""
    if location_id not in known:
        return False, f"unknown_location: {location_id!r}"
    return True, "ok"


def check_event(
    event: Dict[str, Any],
    *,
    expected_sku: str = "",
    known_locations: set,
    cert_ids: list,
    has_work_order: bool = True,
    item_expiry: Dict[str, str] = None,
    candidate: str = "",
    temp_c: float = None,
) -> Tuple[bool, str]:
    """Run the full rulebook for one event payload (order matters)."""
    sku = str(event.get("sku", ""))
    to_ref = str(event.get("to", ""))
    event_type = str(event.get("event_type", ""))

    if expected_sku and sku != expected_sku:
        return False, f"sku_mismatch: scanned {sku!r} != expected {expected_sku!r}"
    if not certificate_required(sku, cert_ids)[0]:
        return certificate_required(sku, cert_ids)
    # Temperature-sensitive SKUs may not be stored/moved out of the band.
    if temp_c is not None and event_type in ("receive", "store", "return"):
        for prefix in TEMPERATURE_SENSITIVE_SKUS:
            if sku.upper().startswith(prefix):
                ok, reason = temperature_in_range(temp_c)
                if not ok:
                    return ok, reason
    # The target of receive/store/return is addressable storage; issue goes to
    # a well/crew, not a cell.
    if event_type in ("receive", "store", "return") and not location_known(
        to_ref, known_locations
    )[0]:
        return location_known(to_ref, known_locations)
    if event_type == "issue" and not issue_authorized(has_work_order)[0]:
        return issue_authorized(has_work_order)
    if event_type == "issue" and candidate and item_expiry:
        ok, reason = fefo_issue(item_expiry, candidate)
        if not ok:
            return ok, reason
    return True, "ok"
