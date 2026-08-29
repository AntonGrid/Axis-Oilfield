"""Axis-Oilfield — domain profile: MTR inventory chain-of-custody.

Pure domain logic (stdlib only), independent of the Axis Core crypto layer —
the trust pipeline (Ed25519 signing/verification) is applied by consumers via
``axis_core`` (see ``examples/oilfield_inventory.py``).

Modules:
- ``model``   — entities: sites, locations, items, events, snapshots;
- ``policies``— the warehouse rulebook (SKU match, FEFO, temperature, certs);
- ``custody`` — the registry: apply events, query the «куст map», detect
  custody breaks, produce inventory discrepancy reports.
"""

from oilfield.custody import InventoryRegistry
from oilfield.model import InventoryEvent, Item, Location, Site

__all__ = [
    "InventoryEvent",
    "InventoryRegistry",
    "Item",
    "Location",
    "Site",
]
