"""oilfield — CLI for daily pad/warehouse work (offline-first).

Stores all state in one JSON file (default ``~/.oilfield/state.json``):
sites, locations, items, certificates, lot expiry, signed events, signed
inventory snapshots and the keepers' Ed25519 keys (seed). On every run the
registry is rebuilt by replaying the signed event log — deterministic.

Example day:
    oilfield init
    oilfield site add WH-01 "Склад" --region База
    oilfield loc  add WH-01:ряд-А:стеллаж-1:яч-1 --site WH-01 --kind cell
    oilfield item add tube-01 --sku HKT-73 --serial SN-88213 --batch B-2201
    oilfield keeper add Иван
    oilfield receive tube-01 WH-01:ряд-А:стеллаж-1:яч-1 --keeper Иван
    oilfield move   tube-01 PAD-12:конт-Б:стеллаж-3:яч-7 --keeper Иван
    oilfield snapshot PAD-12:конт-Б:стеллаж-3:яч-7 --item tube-01 --keeper Иван
    oilfield report PAD-12:конт-Б:стеллаж-3:яч-7
    oilfield find SN-88213
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from typing import Any, Dict, List, Optional

import nacl.signing

from oilfield import InventoryRegistry, InventorySnapshot
from oilfield.model import Certificate, InventoryEvent, Item
from oilfield.sync import EventOutbox

# ─────────────────────────────── state store ───────────────────────────────

DEFAULT_DATA = os.path.expanduser("~/.oilfield/state.json")


class Store:
    """Load/save/rebuild the domain state from one JSON file."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.data: Dict[str, Any] = {
            "sites": {},
            "locations": {},
            "items": {},
            "certificates": {},
            "lot_expiry": {},
            "events": [],
            "snapshots": [],
            "keepers": {},
        }

    def load(self) -> None:
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as fh:
                merged = json.load(fh)
            for key in self.data:
                if key in merged:
                    self.data[key] = merged[key]

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=1)

    # ── rebuild ──────────────────────────────────────────────────────────────

    def rebuild(self) -> InventoryRegistry:
        """Replay the signed event log into a fresh registry."""
        reg = InventoryRegistry()
        d = self.data
        for site_id, s in d["sites"].items():
            reg.add_site(site_id, s.get("name", ""), s.get("region", ""))
        for loc_id, l in d["locations"].items():
            reg.add_location(loc_id, l["site_id"], l.get("kind", "area"))
        for item_id, i in d["items"].items():
            reg.add_item(
                Item(
                    item_id=item_id,
                    sku=i["sku"],
                    serial_no=i.get("serial_no"),
                    batch_no=i.get("batch_no"),
                    qty=i.get("qty", 1.0),
                    cert_ids=list(i.get("cert_ids", [])),
                )
            )
        for cert_id, c in d["certificates"].items():
            reg.add_certificate(Certificate(cert_id=cert_id, sku=c["sku"]))
        for batch, expiry in d["lot_expiry"].items():
            reg.set_lot_expiry(batch, expiry)
        for raw in d["events"]:
            reg.apply(InventoryEvent.from_dict(raw))
        for raw in d["snapshots"]:
            reg.record_snapshot(InventorySnapshot.from_dict(raw))
        return reg

    def sync_registry(self, reg: InventoryRegistry) -> None:
        """Persist registry back into the store (events appended by CLI)."""
        self.data["events"] = [e.to_dict() for e in reg.events]
        self.data["snapshots"] = [s.to_dict() for s in reg.snapshots]
        items = {}
        for item_id, it in reg.items.items():
            items[item_id] = {
                "sku": it.sku,
                "serial_no": it.serial_no,
                "batch_no": it.batch_no,
                "qty": it.qty,
                "cert_ids": it.cert_ids,
            }
        self.data["items"] = items

    # ── keepers ─────────────────────────────────────────────────────────────

    def add_keeper(self, name: str) -> Dict[str, str]:
        key = nacl.signing.SigningKey.generate()
        entry = {
            "public_key_b64": base64.b64encode(bytes(key.verify_key)).decode(),
            "seed_b64": base64.b64encode(bytes(key)).decode(),
        }
        self.data["keepers"][name] = entry
        return entry

    def keeper_key(self, name: str) -> nacl.signing.SigningKey:
        entry = self.data["keepers"].get(name)
        if entry is None:
            raise SystemExit(f"❌ кладовщик «{name}» не найден (oilfield keeper add {name})")
        return nacl.signing.SigningKey(base64.b64decode(entry["seed_b64"]))


# ─────────────────────────────── helpers ────────────────────────────────────

def _now() -> str:
    import datetime as dt

    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _keeper(store, name):
    entry = store.data["keepers"].get(name)
    if not entry:
        raise SystemExit(f"❌ кладовщик «{name}» не найден (oilfield keeper add {name})")
    key = nacl.signing.SigningKey(base64.b64decode(entry["seed_b64"]))
    return key, entry["public_key_b64"]


def _signature(key, obj) -> str:
    from axis_core.signature_utils import sign_proof

    return sign_proof(key, obj.to_payload())


def _next_nonce(reg, actor_b64) -> int:
    return max([e.nonce for e in reg.events if e.actor == actor_b64] or [0]) + 1


def _item(reg, item_id) -> Item:
    item = reg.items.get(item_id)
    if item is None:
        raise SystemExit(f"❌ позиция «{item_id}» не найдена (oilfield item add {item_id} …)")
    return item


def _make_event(event_type, item, to_ref, actor_b64) -> InventoryEvent:
    return InventoryEvent(
        event_type=event_type,
        item_id=item.item_id,
        sku=item.sku,
        to_ref=to_ref,
        actor=actor_b64,
        certificates=list(item.cert_ids),
    )


# ─────────────────────────────── commands ───────────────────────────────────

def cmd_init(store, args) -> None:
    store.load()
    store.save()
    print(f"✅ хранилище создано: {store.path}")


def cmd_site_add(store, args) -> None:
    store.load()
    store.data["sites"][args.site_id] = {"name": args.name, "region": args.region}
    store.save()
    print(f"✅ площадка {args.site_id} ({args.name})")


def cmd_loc_add(store, args) -> None:
    store.load()
    store.data["locations"][args.location_id] = {"site_id": args.site, "kind": args.kind}
    store.save()
    print(f"✅ адрес {args.location_id} (site={args.site}, kind={args.kind})")


def cmd_item_add(store, args) -> None:
    store.load()
    store.data["items"][args.item_id] = {
        "sku": args.sku,
        "serial_no": args.serial,
        "batch_no": args.batch,
        "qty": args.qty,
        "cert_ids": args.cert or [],
    }
    store.save()
    print(f"✅ позиция {args.item_id} ({args.sku})")


def cmd_cert_add(store, args) -> None:
    store.load()
    store.data["certificates"][args.cert_id] = {"sku": args.sku}
    store.save()
    print(f"✅ сертификат {args.cert_id} → {args.sku}")


def cmd_lot_expiry(store, args) -> None:
    store.load()
    store.data["lot_expiry"][args.batch] = args.expiry
    store.save()
    print(f"✅ партия {args.batch} годна до {args.expiry}")


def cmd_keeper(store, args) -> None:
    store.load()
    if args.action == "add":
        entry = store.add_keeper(args.name)
        store.save()
        print(f"✅ кладовщик «{args.name}» создан")
        print(f"   публичный ключ: {entry['public_key_b64']}")
    elif args.action == "seed":
        entry = store.data["keepers"].get(args.name)
        if not entry:
            raise SystemExit(f"❌ кладовщик «{args.name}» не найден")
        print("⚠ СЕКРЕТ! Вставляй только в свой сканер (web/index.html):")
        print(entry["seed_b64"])
    else:
        for name, e in store.data["keepers"].items():
            print(f"  {name}: {e['public_key_b64'][:20]}…")


def _apply_move(store, args, event_type, label, expected_sku="") -> None:
    store.load()
    reg = store.rebuild()
    key, actor = _keeper(store, args.keeper)
    item = _item(reg, args.item_id)
    ev = _make_event(event_type, item, args.to_ref, actor)
    ev.ts = _now()
    ev.nonce = _next_nonce(reg, actor)
    ev.signature = _signature(key, ev)
    outcome = reg.apply(ev, expected_sku=expected_sku, has_work_order=True)
    if not outcome.accepted:
        print(f"⛔ {label} отклонено: {outcome.reason}")
        return
    store.sync_registry(reg)
    store.save()
    print(f"✅ {label}: {item.item_id} → {args.to_ref} (подписано {actor[:8]}…)")


def cmd_receive(store, args) -> None:
    _apply_move(store, args, "receive", "приёмка")


def cmd_move(store, args) -> None:
    _apply_move(store, args, "store", "перемещение")


def cmd_issue(store, args) -> None:
    _apply_move(store, args, "issue", "выдача", expected_sku=args.expected_sku or "")


def cmd_return(store, args) -> None:
    _apply_move(store, args, "return", "возврат")


def cmd_snapshot(store, args) -> None:
    store.load()
    reg = store.rebuild()
    key, actor = _keeper(store, args.keeper)
    obs = {}
    for item_id in args.item or []:
        it = _item(reg, item_id)
        obs[item_id] = {"sku": it.sku, "qty": it.qty, "serial_no": it.serial_no or ""}
    snap = InventorySnapshot(
        snapshot_id=f"inv-{len(reg.snapshots) + 1}",
        location_id=args.location,
        actor=actor,
        ts=_now(),
        observations=obs,
    )
    snap.nonce = max([s.nonce for s in reg.snapshots if s.actor == actor] or [0]) + 1
    snap.signature = _signature(key, snap)
    outcome = reg.record_snapshot(snap)
    if not outcome.accepted:
        print(f"⛔ снэпшот отклонён: {outcome.reason}")
        return
    store.sync_registry(reg)
    store.save()
    print(f"✅ снэпшот подписан: {args.location} — предметов {len(obs)}")


def cmd_report(store, args) -> None:
    store.load()
    reg = store.rebuild()
    rows = reg.inventory_report(args.location)
    if not rows:
        print("  ✅ расхождений нет")
    for row in rows:
        if row.get("anomaly") == "item_missing_on_shelf":
            print(f"  ⚠ {row['item_id']} {row['sku']} — числится, но на месте НЕТ")
        elif "error" in row:
            print(f"  ℹ {row['error']} — сначала: oilfield snapshot {args.location} --item …")
        else:
            print(
                f"  ⚠ {row['sku']}: ожидалось {row['expected']}, "
                f"по факту {row['actual']} (пересортица)"
            )


def cmd_find(store, args) -> None:
    store.load()
    reg = store.rebuild()
    rows = reg.search(args.term)
    if not rows:
        print("  ничего не найдено")
        return
    for r in rows:
        extra = f" серия {r['serial_no']}" if r.get("serial_no") else ""
        ev = f"  ({r['last_event']})" if r.get("last_event") else ""
        print(f"  {r['item_id']} {r['sku']}{extra} → {r['location']}{ev}")


def cmd_loc(store, args) -> None:
    store.load()
    reg = store.rebuild()
    rows = reg.location_contents(args.location)
    if not rows:
        print("  ячейка пуста")
        return
    for r in rows:
        ser = r.get("serial_no") or ""
        print(f"  {r['item_id']} {r['sku']} {ser} (qty {r['qty']})")


def cmd_hist(store, args) -> None:
    store.load()
    reg = store.rebuild()
    rows = reg.history(args.item_id)
    if not rows:
        print("  событий нет")
        return
    for e in rows:
        print(
            f"  {e.ts[:19]}  {e.event_type:<8} {e.from_ref or '—':<24} → "
            f"{e.to_ref or '—'}   actor {e.actor[:8]}…"
        )


def cmd_summary(store, args) -> None:
    store.load()
    reg = store.rebuild()
    for site, info in sorted(reg.stock_summary().items()):
        print(f"  {site}: {info['items']} ед., SKU: {', '.join(info['skus'])}")
    lost = reg.detect_lost_items(days=args.days or 30)
    for l in lost:
        print(f"  ⚠ потеряшка: {l['item_id']} {l['sku']} — без движений {l['days_since']} дн.")


# ─────────────────────────────── QR labels ──────────────────────────────────

def _outdir(args) -> str:
    return getattr(args, "out", None) or "qr_out"


def cmd_qr_items(store, args) -> None:
    store.load()
    reg = store.rebuild()
    from oilfield.qrkit import item_label, write_labels

    items = list(reg.items.values())
    names = [it.item_id for it in items]
    paths = write_labels([item_label(it) for it in items], _outdir(args), names)
    for p in paths:
        print(f"  🖨 {p}")
    print(f"  всего этикеток: {len(paths)}")


def cmd_qr_locs(store, args) -> None:
    store.load()
    reg = store.rebuild()
    from oilfield.qrkit import location_label, write_labels

    locs = sorted(reg.locations)
    if args.site:
        locs = [l for l in locs if l.startswith(args.site + ":")]
    paths = write_labels([location_label(l) for l in locs], _outdir(args), [f"loc_{l}" for l in locs])
    for p in paths:
        print(f"  🖨 {p}")
    print(f"  всего этикеток: {len(paths)}")


def cmd_qr_item(store, args) -> None:
    store.load()
    reg = store.rebuild()
    from oilfield.qrkit import item_label, write_labels

    item = _item(reg, args.item_id)
    paths = write_labels([item_label(item)], _outdir(args), [item.item_id])
    print(f"  🖨 {paths[0]}")


def cmd_serve(store, args) -> None:
    from oilfield.gateway import serve

    serve(store.path, host=args.host, port=args.port)


# ─────────────────────────────── intel (PoI) ────────────────────────────────

def cmd_intel_accuracy(store, args) -> None:
    store.load()
    reg = store.rebuild()
    from oilfield.intel import site_accuracy

    sites = [args.site] if args.site else sorted(reg.sites)
    for s in sites:
        a = site_accuracy(reg, s)
        if a is None:
            print(f"  {s}: нет подписанных снэпшотов — точность неизвестна")
        else:
            print(f"  {s}: точность {a['accuracy']:.1%} "
                  f"(снэпшотов локаций: {a['locations_checked']}, единиц: {a['units_checked']})")


def cmd_intel_demand(store, args) -> None:
    store.load()
    reg = store.rebuild()
    from oilfield.intel import demand_profile

    skus = [args.sku] if args.sku else sorted({i.sku for i in reg.items.values()})
    for sku in skus:
        prof = demand_profile(reg, sku, days=args.days)
        if not prof:
            print(f"  {sku}: выдач за {args.days} дн. нет")
            continue
        for site, p in sorted(prof.items()):
            print(f"  {sku} @ {site}: выдано {p['issued']}, "
                  f"~{p['daily_avg']}/день → на {p['horizon_need']} на горизонте")


def cmd_intel_contribution(store, args) -> None:
    store.load()
    reg = store.rebuild()
    from oilfield.intel import build_contribution
    import json

    key, actor = _keeper(store, args.keeper)
    c = build_contribution(reg, site_id=args.site, actor=actor, key=key,
                           days=args.days, skus=([args.sku] if args.sku else None))
    payload = c.to_dict()
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        print(f"✅ вклад {args.site} подписан → {args.out}")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=1))


def cmd_intel_aggregate(store, args) -> None:
    from oilfield.intel import SiteContribution, aggregate
    import json

    cons = []
    for path in args.files:
        with open(path, encoding="utf-8") as fh:
            cons.append(SiteContribution.from_dict(json.load(fh)))
    for sku, info in sorted(aggregate(cons).items()):
        sites = ", ".join(f"{k}: {v}" for k, v in info["sites"].items())
        print(f"  {sku}: взвешенное за период {info['weighted_period_qty']} | {sites}")


def cmd_intel_rebalance(store, args) -> None:
    store.load()
    reg = store.rebuild()
    from oilfield.intel import SiteContribution, rebalance_suggestions
    import json

    cons = []
    for path in args.files:
        with open(path, encoding="utf-8") as fh:
            cons.append(SiteContribution.from_dict(json.load(fh)))
    sugg = rebalance_suggestions(reg, cons)
    if not sugg:
        print("  ✅ дефицитов по прогнозу нет")
        return
    for s in sugg:
        print(f"  ⚠ {s['site']} {s['sku']}: в наличии {s['available']}, "
              f"прогноз {s['forecast_need']} → дефицит {s['deficit']}")




# ─────────────────────────────── parser & main ──────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="oilfield", description="Axis-Oilfield CLI — склад МТР")
    p.add_argument("--data", default=DEFAULT_DATA, help="файл хранилища (JSON)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="создать хранилище").set_defaults(fn=cmd_init)

    sp = sub.add_parser("site", help="площадки (склад, кусты)")
    a = sp.add_subparsers(dest="action", required=True)
    ap = a.add_parser("add")
    ap.add_argument("site_id"); ap.add_argument("name"); ap.add_argument("--region", default="")
    ap.set_defaults(fn=cmd_site_add)

    sp = sub.add_parser("loc", help="адреса хранения")
    a = sp.add_subparsers(dest="action", required=True)
    ap = a.add_parser("add")
    ap.add_argument("location_id"); ap.add_argument("--site", required=True); ap.add_argument("--kind", default="cell")
    ap.set_defaults(fn=cmd_loc_add)

    sp = sub.add_parser("item", help="позиции МТР")
    a = sp.add_subparsers(dest="action", required=True)
    ap = a.add_parser("add")
    ap.add_argument("item_id"); ap.add_argument("--sku", required=True)
    ap.add_argument("--serial"); ap.add_argument("--batch")
    ap.add_argument("--qty", type=float, default=1.0)
    ap.add_argument("--cert", action="append", default=[])
    ap.set_defaults(fn=cmd_item_add)

    sp = sub.add_parser("cert", help="сертификаты")
    a = sp.add_subparsers(dest="action", required=True)
    ap = a.add_parser("add")
    ap.add_argument("cert_id"); ap.add_argument("--sku", required=True)
    ap.set_defaults(fn=cmd_cert_add)

    sp = sub.add_parser("lot", help="партии")
    a = sp.add_subparsers(dest="action", required=True)
    ap = a.add_parser("expiry")
    ap.add_argument("batch"); ap.add_argument("expiry")
    ap.set_defaults(fn=cmd_lot_expiry)

    sp = sub.add_parser("keeper", help="кладовщики")
    a = sp.add_subparsers(dest="action", required=True)
    ap = a.add_parser("add"); ap.add_argument("name"); ap.set_defaults(fn=cmd_keeper)
    ap = a.add_parser("list"); ap.set_defaults(fn=cmd_keeper)
    ap = a.add_parser("seed"); ap.add_argument("name"); ap.set_defaults(fn=cmd_keeper)

    for name, fn, target in [("receive", cmd_receive, "приёмка (куда)"),
                             ("move", cmd_move, "переместить (куда)"),
                             ("issue", cmd_issue, "выдать (скважина/наряд)"),
                             ("return", cmd_return, "вернуть (куда)")]:
        ap = sub.add_parser(name, help=f"{target}")
        ap.add_argument("item_id"); ap.add_argument("to_ref")
        ap.add_argument("--expected-sku", default="")
        ap.add_argument("--keeper", required=True)
        ap.set_defaults(fn=fn)

    ap = sub.add_parser("snapshot", help="подписанная инвентаризация ячейки")
    ap.add_argument("location")
    ap.add_argument("--item", action="append", default=[])
    ap.add_argument("--keeper", required=True)
    ap.set_defaults(fn=cmd_snapshot)

    sub.add_parser("report", help="пересортица по подписанному снэпшоту").add_argument("location")
    sub.add_parser("find", help="найти по серии/артикулу/партии").add_argument("term")
    sub.add_parser("at", help="что лежит в ячейке").add_argument("location")
    sub.add_parser("hist", help="история позиции").add_argument("item_id")
    sub.add_parser("summary", help="карта кустов + потеряшки").add_argument("--days", type=int)

    sp = sub.add_parser("qr", help="QR-этикетки для печати")
    a = sp.add_subparsers(dest="action", required=True)
    ap = a.add_parser("items", help="этикетки всех позиций"); ap.add_argument("--out", default=None)
    ap.set_defaults(fn=cmd_qr_items)
    ap = a.add_parser("item", help="этикетка одной позиции")
    ap.add_argument("item_id"); ap.add_argument("--out", default=None)
    ap.set_defaults(fn=cmd_qr_item)
    ap = a.add_parser("locs", help="этикетки ячеек")
    ap.add_argument("--site", default=None); ap.add_argument("--out", default=None)
    ap.set_defaults(fn=cmd_qr_locs)

    sp = sub.add_parser("serve", help="HTTP-шлюз для сканов с телефона")
    sp.add_argument("--host", default="0.0.0.0"); sp.add_argument("--port", type=int, default=8080)
    sp.set_defaults(fn=cmd_serve)

    sp = sub.add_parser("intel", help="PoI: точность/прогноз/вклады площадок")
    a = sp.add_subparsers(dest="action", required=True)
    ap = a.add_parser("accuracy"); ap.add_argument("--site", default=None)
    ap.set_defaults(fn=cmd_intel_accuracy)
    ap = a.add_parser("demand"); ap.add_argument("--sku", default=None)
    ap.add_argument("--days", type=int, default=30); ap.set_defaults(fn=cmd_intel_demand)
    ap = a.add_parser("contribution"); ap.add_argument("--site", required=True)
    ap.add_argument("--keeper", required=True); ap.add_argument("--sku", default=None)
    ap.add_argument("--days", type=int, default=30); ap.add_argument("--out", default=None)
    ap.set_defaults(fn=cmd_intel_contribution)
    ap = a.add_parser("aggregate"); ap.add_argument("files", nargs="+")
    ap.set_defaults(fn=cmd_intel_aggregate)
    ap = a.add_parser("rebalance"); ap.add_argument("files", nargs="+")
    ap.set_defaults(fn=cmd_intel_rebalance)

    leaf = {"report": cmd_report, "find": cmd_find, "at": cmd_loc,
            "hist": cmd_hist, "summary": cmd_summary}
    for name, parser in sub.choices.items():
        if name in leaf:
            parser.set_defaults(fn=leaf[name])
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = Store(args.data)
    store.load()
    args.fn(store, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())



