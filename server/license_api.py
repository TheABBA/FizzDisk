#!/usr/bin/env python3
"""FizzDisk Activation API — one-user, one-device license control.

A tiny, dependency-free HTTP service (stdlib only) that tracks which device a
product key is activated on. The app deactivates (frees the key) on the old
machine, then activates on the new one. Keys are validated with the same HMAC
as fizzdisk/license.py, so only genuine keys are accepted.

Endpoints (POST JSON):
  /activate    {key, fingerprint} -> bind key to this device (or claim it)
  /deactivate  {key, fingerprint} -> free the key if this device owns it
  /status      {key}              -> current binding state
  /health                          -> 200 ok

Data file: FIZZDISK_DATA (default ./data/licenses.json) — a JSON map of
normalized key -> {fingerprint, activated_at}.
"""
import base64, hashlib, hmac, json, os, re, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

SECRET = b"FizzDisk2026-LicKey-UnlimitedBackups-ForLife"
DATA = os.environ.get("FIZZDISK_DATA", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "licenses.json"))
LOCK = threading.Lock()


def normalize(k: str) -> str:
    k = (k or "").strip().upper().replace(" ", "")
    if k.startswith("FZFK-") or k.startswith("FZFK"):
        k = k[4:].lstrip("-")
    return k.replace("-", "")


def validate_key(key: str) -> bool:
    k = normalize(key)
    if len(k) != 16:
        return False
    try:
        raw = base64.b32decode(k)
    except Exception:
        return False
    if len(raw) != 10:
        return False
    body, sig = raw[:5], raw[5:]
    expect = hmac.new(SECRET, body, hashlib.sha256).digest()[:5]
    return hmac.compare_digest(sig, expect)


def _load() -> dict:
    try:
        with open(DATA, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(db: dict) -> None:
    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(db, f)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_OPTIONS(self):
        self._json(200, {})

    def do_GET(self):
        if urlparse(self.path).path in ("/health", "/api/license/health"):
            return self._json(200, {"ok": True})
        return self._json(404, {"ok": False})

    def do_POST(self):
        path = urlparse(self.path).path
        b = self._body()
        key = normalize(b.get("key", ""))
        fp = (b.get("fingerprint", "") or "").strip()
        if path not in ("/activate", "/api/license/activate",
                        "/deactivate", "/api/license/deactivate",
                        "/status", "/api/license/status"):
            return self._json(404, {"ok": False, "error": "not found"})

        # status (key-only)
        if path.endswith("/status"):
            if not validate_key(key):
                return self._json(200, {"ok": True, "valid": False, "key": key, "active": False})
            with LOCK:
                db = _load()
                rec = db.get(key)
            if not rec or not rec.get("fingerprint"):
                return self._json(200, {"ok": True, "valid": True, "key": key, "active": False, "free": True})
            return self._json(200, {"ok": True, "valid": True, "key": key, "active": True,
                                    "fingerprint": rec["fingerprint"], "free": False})

        # activate / deactivate need fingerprint
        if not validate_key(key):
            return self._json(200, {"ok": False, "reason": "invalid_key",
                                    "message": "That key isn't valid. Check the format (FZFK-XXXX-...)."})
        if not fp:
            return self._json(200, {"ok": False, "reason": "no_fingerprint",
                                    "message": "Could not identify this device."})

        with LOCK:
            db = _load()
            rec = db.get(key, {})
            owner = rec.get("fingerprint", "")

            if path.endswith("/deactivate"):
                if owner and owner != fp:
                    return self._json(200, {"ok": True, "already_free": False,
                                            "message": "This license is active on another device; leaving it there."})
                if key in db:
                    db[key] = {"fingerprint": "", "activated_at": rec.get("activated_at", 0)}
                _save(db)
                return self._json(200, {"ok": True, "message": "Deactivated. You can now activate on another machine."})

            # activate
            if owner and owner != fp:
                return self._json(200, {"ok": False, "reason": "active_elsewhere",
                                        "message": "This license is active on another device. Deactivate it there "
                                                   "(Settings \u2192 \u201cMove to another machine?\u201d), then activate here."})
            db[key] = {"fingerprint": fp, "activated_at": time.time()}
            _save(db)
            return self._json(200, {"ok": True, "message": "Activated \u2014 unlimited backups & lifetime updates."})


def main():
    port = int(os.environ.get("PORT", "8130"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("license API on :%d  data=%s" % (port, DATA), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
