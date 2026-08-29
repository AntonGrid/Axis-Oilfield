# Axis-Oilfield

**Domain profile of [Axis Protocol](https://github.com/AntonGrid/Axis-protocol) for
oil & gas inventory management and wellsite logistics.**

Axis-Oilfield is a **cryptographic chain-of-custody layer for МТР**
(material and technical resources) — pipes, ESP/pump units, chemicals,
spare parts, workwear — from the central warehouse to remote well pads
(«кусты»). Every physical event is **signed on the spot**, cannot be
forged or edited later, and is instantly verifiable by anyone.

## Why this profile exists

Classic problems on oil-field warehouses:

- **Пересортица** — physical goods diverge from accounting documents;
  discovered only at periodic inventory, after the damage is done.
- **Distributed storage** — pads («кусты») are hundreds of kilometres apart;
  nobody has a single picture of *what lies where, on which pad, in which
  container, on which pallet*.
- **Chain of custody gaps** — a pipe leaves the warehouse, is "lost" on a pad,
  or a batch without a certificate is issued to a well.
- **Offline sites** — pads often have no stable Internet, so "paper-only"
  remains the default.

Axis-Oilfield solves them with the Axis trust pipeline:
**Identity → Proof → Attestation → Policy** — where the "device" is the
storekeeper's scanner/phone, the "event" is an inventory movement, and the
"policy" is the warehouse rulebook.

## How it works in one sentence

> Scan the item + scan the location → a signed, timestamped, unforgeable
> event is created → policies check it immediately (correct SKU? certificate?
> FEFO? temperature?) → anomalies are flagged → the whole territory becomes
> one queryable, auditable map of МТР.

## Repository layout

```
Axis-Oilfield/
├── docs/SPECIFICATION.md     # full domain specification
├── src/oilfield/             # reusable domain logic (pure Python)
│   ├── model.py              # items, locations, events, snapshots
│   ├── policies.py           # SKU check, FEFO, temperature, certificates
│   └── custody.py            # chains, custody-break detection, inventory reports
├── examples/
│   └── oilfield_inventory.py # end-to-end demo on Axis Core (Ed25519 signing)
└── tests/                    # pytest suite for the domain logic
```

## Quick start

```bash
# 1. Axis Core (the trust implementation) on PYTHONPATH
pip install -e ../Axis-core

# 2. Run the end-to-end demo
python examples/oilfield_inventory.py

# 3. Run the domain-logic tests
python -m pytest tests/ -q
```

The demo covers: signed receipt → placement into addressable storage →
cross-pad move → **пересортица blocked by policy** → custody-break anomaly →
inventory discrepancy report → offline signing with late sync.

## Relation to the ecosystem

| Layer | Repository | Role here |
|---|---|---|
| L0 Standard | [Axis-protocol](https://github.com/AntonGrid/Axis-protocol) | trust standard, ADRs, constitution |
| L1 Reference impl. | [Axis-core](https://github.com/AntonGrid/Axis-core) | Ed25519 signing/verification, policy engine |
| **Domain profile** | **Axis-Oilfield (this repo)** | oil & gas inventory chain-of-custody |
| Intelligence | [ENRG-AI](https://github.com/AntonGrid/ENRG-AI) | pad-level forecasting, PoI reputation |

## License

Apache License 2.0 — see [LICENSE](LICENSE).
