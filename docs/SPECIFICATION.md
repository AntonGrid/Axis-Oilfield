# Axis-Oilfield — Domain Specification

> **Version:** 0.1 · **Status:** draft
> **Profile of:** [Axis Protocol](https://github.com/AntonGrid/Axis-protocol) v1
> (overlay trust standard) · **Reference implementation:** [Axis Core](https://github.com/AntonGrid/Axis-core)

---

## 1. Purpose

**Axis-Oilfield** defines a **cryptographic chain-of-custody profile for
material and technical resources (МТР)** in oil & gas warehouse and wellsite
operations: from the central warehouse to remote well pads («кусты»).

It solves three problems that classical warehouse systems (WMS, 1C) do not:

1. **Пересортица** — physical items diverge from accounting documents; the
   mismatch is found only at periodic inventory, after losses have happened.
2. **Distributed storage** — pads are far apart; nobody has one trustworthy
   picture of *what lies where* across the territory.
3. **Unforgeable custody** — paper records can be lost, edited, or forged;
   there is no independent proof of *who handled what, when*.

The profile reuses the Axis trust pipeline unchanged:

```
Identity (storekeeper/scanner key) → Event (inventory movement) →
Proof (Ed25519 signature) → Attestation (verified, signed record) →
Policy (warehouse rulebook) → Anomaly / Report
```

---

## 2. Design principles

| # | Principle | How it is enforced |
|---|---|---|
| P-1 | **Physical fact over documents** | every movement is captured by scanning at the moment it happens |
| P-2 | **Nothing is editable** | events are signed with Ed25519; history is append-only |
| P-3 | **Correct at the moment, not at inventory** | policies reject a wrong SKU before the item leaves the gate |
| P-4 | **Works offline** | signatures are created locally and synced later |
| P-5 | **Territory is one map** | every pad/container/shelf/cell has an address; items are bound to addresses |
| P-6 | **Reputation for quality** | pads/sites that keep accurate custody earn ERS/PoI weight |

---

## 3. Domain model

### 3.1 Entities

| Entity | Meaning | Key fields |
|---|---|---|
| **Site / Pad («куст»)** | a physical location (warehouse, pad) | `site_id`, `name`, `region` |
| **Location** | addressable storage unit | `location_id`, `site_id`, `kind` (container/shelf/cell/pallet), `parent` |
| **Item (МТР)** | one physical unit or a batch | `item_id`, `sku`, `serial_no`, `batch_no`, `qty` |
| **Certificate** | quality document (pipe cert, chemical passport) | `cert_id`, `sku`, `content_hash` |
| **Event** | a signed inventory movement | `event_id`, `type`, `item_id`, `from`, `to`, `actor`, `ts`, `signature` |
| **Snapshot** | observed reality at an inventory | `snapshot_id`, `location_id`, `items` |

### 3.2 Event types

| Type | Meaning | `from` / `to` |
|---|---|---|
| `arrival` | goods arrive with shipping doc (ТТН) | supplier → site |
| `receive` | storekeeper receives, items bound to a location | ТТН → location |
| `store` | move to another location (addressable storage) | location → location |
| `issue` | issue per work order (наряд) | location → well / crew |
| `return` | unused items returned | well → location |
| `inventory` | scan reality at a location | observation snapshot |

### 3.3 Event payload (canonical, signed)

```json
{
  "algo": "ed25519",
  "device_id": "<base64 public key of the actor>",
  "nonce": "41",
  "timestamp": "2026-08-29T10:00:00Z",
  "payload": {
    "event_type": "receive",
    "item_id": "item-0007",
    "sku": "HKT-73",
    "serial_no": "SN-88213",
    "from": "ttn-2026-08-28",
    "to": "pad-12:container-B:shelf-3:cell-7",
    "qty": 1,
    "certificates": ["cert-7732"]
  },
  "signature": "<base64>"
}
```

The signature covers the canonical JSON of `device_id, nonce, timestamp,
algo, payload` — exactly the Axis Core convention. The domain only changes
the **payload** shape.


---

## 4. Addressable storage (the «куст map»)

Every storage unit has a stable address composed of the location chain:

```
site (pad-12) → container (Б) → shelf (3) → cell (7) → pallet
```

An item is **bound** to an address by a `store`/`receive` event. Querying
«where is HKT-73?» is a look-up over the latest event per item:

```
pad-12:container-B:shelf-3:cell-7 — 14 шт (last event: receive, 2026-08-28, actor K-12)
pad-05:container-A:pallet-2       —  6 шт (last event: store, 2026-08-27, actor K-03)
```

Because every event is signed and append-only, the map cannot silently
disagree with the paperwork.

## 5. Policies (the warehouse rulebook)

Policies are pure functions evaluated at event time. A violation rejects the
event **before** it is accepted:

| Policy | Rule | Violation reason |
|---|---|---|
| `sku_matches` | scanned SKU == SKU in the document (ТТН / order) | `sku_mismatch` (пересортица) |
| `certificate_required` | SKUs from the certificate list must have a valid cert | `certificate_missing` |
| `fefo` | issue chemicals in First-Expired-First-Out order | `fefo_violation` |
| `temperature_range` | temperature-sensitive items stored within range | `temperature_out_of_range` |
| `issue_authorized` | issue requires a work order (наряд) | `issue_not_authorized` |
| `location_exists` | the target address exists in the site graph | `unknown_location` |

## 6. Anomalies (what the observer flags)

Anomalies are **signals**, not decisions (Axis constitution C-1):

| Anomaly | Detection |
|---|---|
| **Custody break** | the last event's `to` ≠ the location claimed for the item («заявили на кусте 5, а последнее перемещение — на куст 2») |
| **Double issue** | an item already issued is scanned again at issue |
| **Inventory discrepancy** | snapshot count ≠ expected count per location (пересортица at inventory) |
| **Temperature breach** | temperature logger value outside range since the last event |
| **Lost item** | item with no event for > N days while not at its stated location |

## 7. Offline operation

Pads may have no stable Internet. Axis-Oilfield keeps the trust model intact:

1. The scanner/phone signs events **locally** (the private key never leaves it).
2. Events are stored in a local outbox and **synced later** to the gateway.
3. Sync is idempotent (nonce/event_id dedup); signatures prove the events were
   not altered in transit or "filled in" later.
4. Late-arriving events may reorder the map, but never rewrite history.

This mirrors the ADR-0001 key-never-leaves-device pattern already used by the
ESP32 proof-sender.

## 8. Reputation (PoI / ERS)

Each site becomes a **Layer-1 node** of the Axis intelligence network:

- a local model learns the site's consumption and loss patterns;
- signed **federated contributions** are weighted by the site's reputation
  (ERS) — accurate custody and useful forecasts earn influence;
- the territory's network learns to predict demand and rebalance МТР between
  pads.

## 9. Axis-Oilfield vs enterprise WMS

| Aspect | WMS (Ozon/WB style) | Axis-Oilfield |
|---|---|---|
| Cost | millions + IT team | phone + QR labels + signing |
| Scale | one large warehouse | any warehouse, incl. 50–500 m² |
| Trust model | central system trusts itself | independent cryptographic audit trail |
| Пересортица | found at inventory | rejected at the moment of scanning |
| Distributed pads | not applicable (single building) | native («куст map») |
| Openness | closed | open standard, domain-agnostic |

## 10. Integration with 1C (УТ / Предприятие)

1C remains the **accounting** system; Axis-Oilfield adds the **physical
confirmation** layer:

- **Parallel**: Axis keeps its own item registry + signed event log; 1C is
  untouched during the pilot.
- **Bridge**: export nomenclature from 1C (OData / file exchange) → reconcile
  with the Axis map → report «пересортица по позициям» automatically.
- **Later**: two-way exchange (Axis events → 1C documents) via the 1C HTTP
  service / external data source.

## 11. Terminology

| RU | EN | Meaning |
|---|---|---|
| МТР | MTR | material and technical resources |
| куст | pad | well site with several wells |
| пересортица | assortment mismatch | physical vs accounting divergence |
| наряд | work order | authorization to issue items |
| ТТН | shipping doc | goods waybill |
| ячейка / стеллаж | cell / shelf | addressable storage units |

## 12. Pilot roadmap

| Phase | Scope | Deliverable |
|---|---|---|
| 0 | pick 1–3 pain SKUs, measure current losses | baseline report |
| 1 | QR labels + phone scanning (receive/issue/inventory) | signed events on the pad |
| 2 | gateway on the warehouse PC + oracle | live «куст map» |
| 3 | policies + anomaly reports | пересортица stopped at the gate |
| 4 | 1C reconciliation report | «пересортица по позициям» automated |
| 5 | PoI: site models + contributions | pads become L1 nodes |
