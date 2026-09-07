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
├── docs/PITCH.md             # short pitch for management (RU)
├── docs/presentation/        # management deck: index.html + PDF
├── src/oilfield/             # reusable domain logic (pure Python)
│   ├── model.py              # items, locations, events, certificates, snapshots
│   ├── policies.py           # SKU check, FEFO, temperature, certificates
│   ├── custody.py            # registry: apply events, «куст map», anomalies,
│   │                         #   signed inventory snapshots, reports
│   ├── sync.py               # offline outbox: sign at the pad, sync later
│   ├── keeper.py             # Axis Core bridge: Storekeeper (sign) / Gateway (verify)
│   ├── qrkit.py              # QR label generation for items & locations
│   ├── cli.py                # the `oilfield` command (see below)
│   └── gateway.py            # HTTP gateway for phone scanner (web/)
├── web/                      # phone scanner UI (camera QR + offline outbox)
├── examples/
│   ├── oilfield_inventory.py # end-to-end demo on Axis Core (Ed25519 signing)
│   └── oilfield_daily.py     # a real pad-storekeeper day (offline-first + sync)
└── tests/                    # pytest suite for the domain logic
```

**Management deck:** open `docs/presentation/index.html` in a browser
(offline, keyboard/arrow navigation) or send `Axis-Oilfield-pitch.pdf`.

## Quick start

```bash
# 1. Install the domain package
pip install -e .                    # or: pip install -e ".[dev]"

# 2. Axis Core (the trust implementation) for the signing demo
pip install -e ../Axis-core         # or: pip install -e ".[core]"

# 3. Run the end-to-end demos
python examples/oilfield_inventory.py   # full chain-of-custody scenario
python examples/oilfield_daily.py       # a storekeeper's day (offline + sync)

# 4. Run the domain-logic tests
python -m pytest tests/ -q              # 31 tests
```

The demo covers: signed receipt → placement into addressable storage →
cross-pad move → **пересортица blocked by policy** → custody-break anomaly →
inventory discrepancy report → offline signing with late sync.

## Daily CLI (для работы на складе)

```bash
# один раз: справочники
oilfield init
oilfield site add WH-01 "Склад" --region База
oilfield site add PAD-12 "Куст 12" --region Север
oilfield loc  add WH-01:ряд-А:стеллаж-1:яч-1 --site WH-01 --kind cell
oilfield item add tube-01 --sku HKT-73 --serial SN-88213 --batch B-2201 --cert cert-7732
oilfield keeper add "Иван"

# каждый день (всё подписывается Ed25519)
oilfield receive tube-01 WH-01:ряд-А:стеллаж-1:яч-1 --keeper Иван   # приёмка
oilfield move   tube-01 PAD-12:конт-Б:стеллаж-3:яч-7  --keeper Иван   # на куст
oilfield issue  tube-02 well-8 --expected-sku HKT-73 --keeper Иван     # выдача
oilfield snapshot PAD-12:конт-Б:стеллаж-3:яч-7 --item tube-01 --keeper Иван
oilfield report PAD-12:конт-Б:стеллаж-3:яч-7     # пересортица по снэпшоту
oilfield find SN-88213                          # «где труба?» за секунду
oilfield at  WH-01:ряд-А:стеллаж-1:яч-1          # что лежит в ячейке
oilfield hist tube-01                           # вся биография единицы
oilfield summary                                # карта кустов + потеряшки
```

Хранилище — JSON (`~/.oilfield/state.json`, ключи кладовщиков локально).
CLI не требует интернета: подпись и очередь работают на кусте офлайн.

## QR-этикетки, шлюз и сканер с телефона

```bash
# 1. Печать этикеток (PNG, готовы к печати)
oilfield qr items                 # все позиции → qr_out/
oilfield qr locs --site WH-01     # ячейки склада
oilfield qr item tube-01          # одна позиция

# 2. Секретный ключ кладовщика — для импорта в сканер на телефоне
oilfield keeper seed "Иван"       # ⚠ секрет; вставляется один раз в web-сканер

# 3. Шлюз на ПК склада (в локальной сети)
oilfield serve --port 8080        # http://<IP-ПК>:8080 — страница сканера
```

Сканер (`web/index.html`) работает на телефоне в той же сети:
- сохраняет ключ кладовщика один раз (localStorage);
- сканирует QR предмета и QR ячейки;
- **подписывает событие Ed25519 прямо на телефоне** (tweetnacl, формат
  идентичен Axis Core — проверено кросс-подписью) и шлёт на шлюз;
- без связи кладёт события в офлайн-очередь и синхронизирует позже.

Проверено end-to-end: `POST /event` с подписью → `accepted: true`;
повтор того же события → `replay` отклонён.

## Relation to the ecosystem

| Layer | Repository | Role here |
|---|---|---|
| L0 Standard | [Axis-protocol](https://github.com/AntonGrid/Axis-protocol) | trust standard, ADRs, constitution |
| L1 Reference impl. | [Axis-core](https://github.com/AntonGrid/Axis-core) | Ed25519 signing/verification, policy engine |
| **Domain profile** | **Axis-Oilfield (this repo)** | oil & gas inventory chain-of-custody |
| Intelligence | [ENRG-AI](https://github.com/AntonGrid/ENRG-AI) | pad-level forecasting, PoI reputation |

## License

Apache License 2.0 — see [LICENSE](LICENSE).
