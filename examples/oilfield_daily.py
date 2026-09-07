"""Axis-Oilfield — a real day of a pad storekeeper (offline-first).

Story: the storekeeper receives НКТ pipes and a chemical on the central
warehouse, drives to pad 12 (no network there), places a pipe and does a
signed inventory count — all offline, into the outbox. Back at the base the
outbox is synced to the gateway registry. Then a work order arrives for the
wrong SKU (пересортица is blocked), and searches answer «where is this
pipe?» instantly.

Run:  python examples/oilfield_daily.py
Requires: axis-core on PYTHONPATH (pip install -e ../Axis-core)
"""

from oilfield import InventoryRegistry, InventorySnapshot
from oilfield.keeper import Gateway, Storekeeper
from oilfield.model import InventoryEvent, Item
from oilfield.sync import EventOutbox


def main() -> None:
    print("=" * 74)
    print("Axis-Oilfield — рабочий день кладовщика (офлайн-first)")
    print("=" * 74)

    # ── Территория: склад + куст 12 ────────────────────────────────────────
    reg = InventoryRegistry()
    reg.add_site("WH-01", "Центральный склад", "База")
    reg.add_site("PAD-12", "Куст 12", "Север")
    reg.add_location("WH-01:ряд-А:стеллаж-1:яч-1", "WH-01", "cell")
    reg.add_location("WH-01:ряд-А:стеллаж-1:яч-2", "WH-01", "cell")
    reg.add_location("WH-01:реагенты:стеллаж-1:яч-1", "WH-01", "cell")
    reg.add_location("PAD-12:конт-Б:стеллаж-3:яч-7", "PAD-12", "cell")

    # НКТ-73: две трубы одной партии B-2201 + реагент.
    reg.add_item(Item(item_id="tube-01", sku="HKT-73", serial_no="SN-88213", batch_no="B-2201", cert_ids=["cert-7732"]))
    reg.add_item(Item(item_id="tube-02", sku="HKT-73", serial_no="SN-88214", batch_no="B-2201", cert_ids=["cert-7732"]))
    reg.add_item(Item(item_id="reag-01", sku="REAGENT-D", qty=1.0, cert_ids=["passport-9901"]))
    reg.set_lot_expiry("B-2201", "2026-09-01")

    keeper = Storekeeper("Иванов, склад")
    gateway = Gateway()
    reg.facts["verify"] = gateway.verify
    reg.facts["verify_snapshot"] = gateway.verify
    box = EventOutbox("pad-scanner-12")

    def ev(event_type, item, to_ref="", expected_sku=""):
        e = InventoryEvent(
            event_type=event_type,
            item_id=item.item_id,
            sku=item.sku,
            to_ref=to_ref,
            actor=keeper.public_key_b64,
            certificates=item.cert_ids,
        )
        keeper.sign(e)
        return reg.apply(e, expected_sku=expected_sku, has_work_order=True)

    # 1. Приёмка на складе (подписано).
    for tube in ("tube-01", "tube-02"):
        it = reg.items[tube]
        o = ev("receive", it, to_ref="WH-01:ряд-А:стеллаж-1:яч-1")
        assert o.accepted, o.reason
    o = ev("receive", reg.items["reag-01"], to_ref="WH-01:реагенты:стеллаж-1:яч-1", )
    assert o.accepted, o.reason
    print("1. Приёмка на базе: 2×НКТ-73 (партия B-2201) + реагент  ✅ подписано")

    # 2. Кладём трубу tube-01 в outbox и «уезжаем на куст без связи».
    move = InventoryEvent(
        event_type="store", item_id="tube-01", sku="HKT-73",
        to_ref="PAD-12:конт-Б:стеллаж-3:яч-7", actor=keeper.public_key_b64,
        certificates=["cert-7732"],
    )
    keeper.sign(move)
    box.add(move)  # подписано локально, сеть не нужна
    print("2. Куст 12 (без интернета): перемещение НКТ-73 подписано локально, в outbox")

    # 3. Инвентаризация на кусте: подписанный снэпшот «что реально вижу».
    snap = InventorySnapshot(
        snapshot_id="inv-pad12-01",
        location_id="PAD-12:конт-Б:стеллаж-3:яч-7",
        actor=keeper.public_key_b64,
        observations={
            "tube-01": {"sku": "HKT-73", "qty": 1.0, "serial_no": "SN-88213"},
        },
    )
    keeper.sign_snapshot(snap)
    box.add_snapshot(snap)
    print("3. Инвентаризация куста 12: снэпшот подписан (офлайн)")

    # 4. Вернулись на базу — синхронизация outbox.
    res = box.sync(reg)
    print(f"4. Синхронизация с базой: sync={res.synced} skip={res.skipped} reject={res.rejected}")

    # 5. Пересортица: наряд на REAGENT-D, а сканируют трубу → блок.
    o = ev("issue", reg.items["tube-02"], to_ref="well-8", expected_sku="REAGENT-D")
    print(f"5. Наряд на реагент, сканируют трубу   ⛔ {o.reason}")

    # 6. Выдача по правильному наряду проходит (FEFO партии ок).
    o = ev("issue", reg.items["tube-02"], to_ref="well-8", expected_sku="HKT-73")
    print(f"6. Выдача НКТ-73 по наряду             {'✅ выдано' if o.accepted else o.reason}")

    # 7. «Где труба SN-88213?» — поиск за секунду.
    print("7. Поиск «SN-88213»:")
    for row in reg.search("SN-88213"):
        print(f"   → {row['item_id']} {row['sku']} @ {row['location']}")

    # 8. Карта кустов.
    print("8. Карта «что где лежит»:")
    for site, info in sorted(reg.stock_summary().items()):
        print(f"   {site}: {info['items']} ед., SKU: {', '.join(info['skus'])}")

    # 9. Инвентаризация куста: нашли «лишнюю» трубу tube-02 (по документам её выдали,
    #    а физически она на кусте) → пересортица видна сразу.
    snap2 = InventorySnapshot(
        snapshot_id="inv-pad12-02",
        location_id="PAD-12:конт-Б:стеллаж-3:яч-7",
        actor=keeper.public_key_b64,
        observations={
            "tube-01": {"sku": "HKT-73", "qty": 1.0, "serial_no": "SN-88213"},
            "tube-02": {"sku": "HKT-73", "qty": 1.0, "serial_no": "SN-88214"},
        },
    )
    keeper.sign_snapshot(snap2)
    assert reg.record_snapshot(snap2).accepted
    print("9. Инвентаризация куста 12 (подписанный снэпшот):")
    for r in reg.inventory_report("PAD-12:конт-Б:стеллаж-3:яч-7"):
        if "item_missing_on_shelf" in str(r.get("anomaly", "")):
            print(f"   ⚠ {r['item_id']} {r['sku']} числится, но на месте НЕТ")
        elif "diff" in r:
            print(f"   ⚠ {r['sku']}: ожидалось {r['expected']}, по факту {r['actual']} (пересортица)")

    print("\nВсе события и снэпшоты подписаны Ed25519 и проверены Gateway (Axis Core).")


if __name__ == "__main__":
    main()
