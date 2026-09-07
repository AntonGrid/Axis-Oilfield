"""HTTP gateway for Axis-Oilfield.

The store PC runs ``oilfield serve``; phones / scanners POST signed events
(see ``web/``) and the gateway verifies them with Axis Core, applies them to
the registry and persists the state. GET endpoints serve the scanner page
(``web/``) so the phone only needs the store PC's LAN address.

API
---
- ``GET  /health``      → ``{"ok": true, "items": N, "events": M}``
- ``GET  /``            → scanner UI (web/index.html)
- ``GET  /vendor/<f>``  → static JS libs
- ``POST /event``       → one signed event (wire format)
- ``POST /batch``       → ``{"events": [...]}`` signed events
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

from oilfield.cli import Store
from oilfield.keeper import Gateway as VerifyGateway
from oilfield.model import InventoryEvent, InventorySnapshot

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "web")


def _static(path: str) -> Optional[bytes]:
    """Serve a file from the web/ directory only (no path traversal)."""
    root = os.path.realpath(WEB_DIR)
    target = os.path.realpath(os.path.join(root, path))
    if not target.startswith(root) or not os.path.isfile(target):
        return None
    with open(target, "rb") as fh:
        return fh.read()


class _Handler(BaseHTTPRequestHandler):
    store: Store
    verifier: VerifyGateway

    def log_message(self, fmt, *args) -> None:  # keep the console quiet
        pass

    def _json(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code: int, content: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    # ── routes ───────────────────────────────────────────────────────────────

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/health":
            reg = self.store.rebuild()
            return self._json(200, {"ok": True, "items": len(reg.items),
                                    "events": len(reg.events),
                                    "snapshots": len(reg.snapshots)})
        if path == "/" or path == "/index.html":
            content = _static("index.html")
            if content is not None:
                return self._text(200, content, "text/html; charset=utf-8")
            return self._json(404, {"error": "web/index.html not built"})
        if path.startswith("/vendor/"):
            content = _static(path.lstrip("/"))
            if content is not None:
                ctype = "application/javascript" if path.endswith(".js") else "application/octet-stream"
                return self._text(200, content, ctype)
            return self._json(404, {"error": "no such asset"})
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return self._json(400, {"accepted": False, "reason": "bad_json"})

        if self.path == "/event":
            return self._json(*self._apply_one(payload))
        if self.path == "/batch":
            results = [self._apply_one(e)[1] for e in payload.get("events", [])]
            return self._json(200, {"results": results})
        self._json(404, {"error": "not found"})

    # ── event application ────────────────────────────────────────────────────

    def _apply_one(self, raw: Dict[str, Any]):
        reg = self.store.rebuild()
        kind = raw.get("kind", "event")
        actor = raw.get("device_id", "")
        if actor not in {k["public_key_b64"] for k in self.store.data["keepers"].values()}:
            return 403, {"accepted": False, "reason": "unknown_keeper"}
        if kind == "snapshot":
            snap = InventorySnapshot.from_dict(raw)
            if "verify_snapshot" not in reg.facts:
                reg.facts["verify_snapshot"] = self.verifier.verify
            outcome = reg.record_snapshot(snap)
        else:
            event = InventoryEvent.from_dict(raw)
            if "verify" not in reg.facts:
                reg.facts["verify"] = self.verifier.verify
            outcome = reg.apply(event, has_work_order=True)
        if outcome.accepted:
            self.store.sync_registry(reg)
            self.store.save()
        return 200, {"accepted": outcome.accepted, "reason": outcome.reason}


def serve(data_path: str, host: str = "0.0.0.0", port: int = 8080) -> None:
    store = Store(data_path)
    store.load()
    server = ThreadingHTTPServer((host, port), _Handler)
    _Handler.store = store
    _Handler.verifier = VerifyGateway()
    print(f"🌐 Axis-Oilfield gateway: http://{host}:{port}  (data: {data_path})")
    print(f"   На телефоне в той же сети: http://<IP-ПК>:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлено")


if __name__ == "__main__":
    serve(os.path.expanduser("~/.oilfield/state.json"))
