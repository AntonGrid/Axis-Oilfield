"""QR labels for МТР items and storage locations.

The QR payload is a small canonical JSON so any scanner (phone, terminal)
can read the same content that the CLI uses:

- item:     ``{"of":"item","id":"tube-01","sku":"HKT-73","serial":"SN-88213"}``
- location: ``{"of":"loc","id":"WH-01:ряд-А:стеллаж-1:яч-1"}``
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
FONT_R = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

QR_SIZE = 340
PAD = 24
CAPTION_H = 120
LABEL_W = QR_SIZE + PAD * 2 + 340  # QR слева, текст справа
LABEL_H = QR_SIZE + PAD * 2 + CAPTION_H


def item_payload(item) -> Dict[str, str]:
    return {
        "of": "item",
        "id": item.item_id,
        "sku": item.sku,
        "serial": item.serial_no or "",
        "batch": item.batch_no or "",
        "certs": ",".join(item.cert_ids),
    }


def location_payload(location_id: str) -> Dict[str, str]:
    return {"of": "loc", "id": location_id}


def _label_image(payload: Dict[str, str], title: str, subtitle: str) -> Image.Image:
    import qrcode

    qr = qrcode.QRCode(border=1, box_size=8)
    qr.add_data(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    # scale QR to fixed box
    qr_img = qr_img.resize((QR_SIZE, QR_SIZE), Image.LANCZOS)

    img = Image.new("RGB", (LABEL_W, LABEL_H), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, LABEL_W - 1, LABEL_H - 1], outline=(0, 0, 0), width=3)
    img.paste(qr_img, (PAD, PAD))

    x = PAD * 2 + QR_SIZE
    f_big = ImageFont.truetype(FONT, 44)
    f_mid = ImageFont.truetype(FONT_R, 34)
    d.text((x, PAD), title, font=f_big, fill=(0, 0, 0))
    d.text((x, PAD + 70), subtitle, font=f_mid, fill=(60, 60, 60))

    cap = f"oilfield find {payload.get('id', '')}"
    d.text((PAD, LABEL_H - CAPTION_H), cap, font=ImageFont.truetype(FONT_R, 26), fill=(80, 80, 80))
    return img


def item_label(item) -> Image.Image:
    pl = item_payload(item)
    return _label_image(pl, f"{item.item_id}  ·  {item.sku}",
                        f"серия {item.serial_no}" if item.serial_no else item.sku)


def location_label(location_id: str) -> Image.Image:
    pl = location_payload(location_id)
    return _label_image(pl, location_id.split(":")[-1], location_id)


def write_labels(
    images: List[Image.Image],
    outdir: str,
    names: List[str],
) -> List[str]:
    os.makedirs(outdir, exist_ok=True)
    paths = []
    for img, name in zip(images, names):
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        path = os.path.join(outdir, f"{safe}.png")
        img.save(path)
        paths.append(path)
    return paths
