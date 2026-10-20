#!/usr/bin/env python3
"""FizzDisk Activation API — one-user, one-device license control + download stats.

A tiny, dependency-free HTTP service (stdlib only) that tracks which device a
product key is activated on, AND counts installer downloads per released version
so the owner can see uptake on a private stats page. The app deactivates (frees
the key) on the old machine, then activates on the new one. Keys are validated
with the same HMAC as fizzdisk/license.py, so only genuine keys are accepted.

Endpoints:
  /activate    {key, fingerprint} -> bind key to this device (or claim it)
  /deactivate  {key, fingerprint} -> free the key if this device owns it
  /status      {key}              -> current binding state
  /health                          -> 200 ok
  DL count ('?ver=' optional, defaults to 'unknown'):
  /dl                               -> increments the counter for that version
  Private (Basic auth; NOT public):
  /stats       -> HTML page of downloads-per-version
  /stats.json  -> JSON of downloads-per-version

Data files (under FIZZDISK_DATA's dir by default):
  licenses.json   : normalized key -> {fingerprint, activated_at}
  downloads.json  : version -> download count

Private-page auth: set FIZZDISK_STATS_USER + FIZZDISK_STATS_PASS. If either is
unset the stats page is open (convenient for local dev).
"""
import base64, hashlib, hmac, json, os, re, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

SECRET = b"FizzDisk2026-LicKey-UnlimitedBackups-ForLife"
DATA = os.environ.get("FIZZDISK_DATA", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "licenses.json"))
DOWNLOADS = os.environ.get("FIZZDISK_DOWNLOADS", os.path.join(os.path.dirname(DATA), "downloads.json"))
STATS_USER = os.environ.get("FIZZDISK_STATS_USER", "")
STATS_PASS = os.environ.get("FIZZDISK_STATS_PASS", "")
LOCK = threading.Lock()

BRAND = "#f59e0b"


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


def _load_dl() -> dict:
    try:
        with open(DOWNLOADS, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_dl(db: dict) -> None:
    os.makedirs(os.path.dirname(DOWNLOADS), exist_ok=True)
    with open(DOWNLOADS, "w", encoding="utf-8") as f:
        json.dump(db, f)


def _html_escape(s) -> str:
    return ("" if s is None else str(s)).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _version_key(ver: str):
    """Sort key for semver-like version strings: (major, minor, patch, extra)."""
    parts = []
    for p in str(ver).split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts + [0, 0, 0, 0][:4])


def _stats_html(db: dict) -> str:
    """Self-contained brand-styled HTML stats page (server-rendered).

    Sorted by version (newest first, oldest at bottom) — not by download count.
    """
    items = sorted(db.items(), key=lambda kv: _version_key(kv[0]), reverse=True)
    total = sum(db.values())
    now = time.strftime("%Y-%m-%d %H:%M %Z")
    rows = []
    for ver, cnt in items:
        pct = (cnt / total * 100) if total else 0
        rows.append(
            "<tr>"
            f"<td class='ver'>{_html_escape(ver)}</td>"
            f"<td class='num'>{cnt:,}</td>"
            f"<td class='barbar'><div class='bar' style='width:{pct:.1f}%'></div></td>"
            f"<td class='pct'>{pct:.1f}%</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='4' class='empty'>No downloads recorded yet.</td></tr>")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FizzDisk \u2014 Download stats</title>
<style>
  :root {{ --brand:{BRAND}; --bg:#0f1115; --card:#171a21; --fg:#e6e8ee; --mut:#9aa0ab; --line:#2a2f3a; }}
  * {{ box-sizing:border-box; }} body {{ margin:0; font-family:system-ui,"Segoe UI",Roboto,sans-serif;
      background:var(--bg); color:var(--fg); }} .wrap {{ max-width:820px; margin:40px auto; padding:0 20px; }}
  h1 {{ font-size:22px; margin:0 0 4px; }} h1 b {{ color:var(--brand); }}
  .sub {{ color:var(--mut); margin:0 0 22px; font-size:14px; }}
  .cards {{ display:flex; gap:14px; margin:0 0 22px; }}
  .card {{ flex:1; background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px 18px; }}
  .card .k {{ color:var(--mut); font-size:12px; text-transform:uppercase; letter-spacing:.4px; }}
  .card .v {{ font-size:28px; font-weight:700; margin-top:4px; }} .card .v .u {{ font-size:14px; color:var(--mut); font-weight:500; }}
  table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:12px; overflow:hidden; }}
  th,td {{ text-align:left; padding:11px 14px; font-size:14px; }}
  th {{ color:var(--mut); font-weight:600; font-size:12px; text-transform:uppercase; letter-spacing:.4px; border-bottom:1px solid var(--line); }}
  td.num {{ font-variant-numeric:tabular-nums; width:110px; }} td.ver {{ font-family:ui-monospace,Consolas,monospace; }}
  .bar {{ height:10px; border-radius:6px; background:var(--brand); opacity:.85; }}
  .barbar {{ min-width:120px; }} td.pct {{ color:var(--mut); width:80px; font-variant-numeric:tabular-nums; }}
  tr + tr td {{ border-top:1px solid var(--line); }} .empty {{ color:var(--mut); text-align:center; padding:26px; }}
  .up {{ color:var(--mut); font-size:12px; margin-top:18px; }}
</style></head><body><div class="wrap">
  <h1>FizzDisk <b>&#9679;</b> Download stats</h1>
  <p class="sub">Installer downloads, grouped by released version. Private \u2014 not linked from the public site.</p>
  <div class="cards">
    <div class="card"><div class="k">Total downloads</div><div class="v">{total:,}</div></div>
    <div class="card"><div class="k">Versions served</div><div class="v">{len(items)}</div></div>
  </div>
  <table><thead><tr><th>Version</th><th>Downloads</th><th></th><th>Share</th></tr></thead>
  <tbody>{''.join(rows)}</tbody></table>
  <p class="up">Recorded {now}</p>
</div></body></html>"""


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

    def _html(self, code, body):
        body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self) -> bool:
        if not STATS_USER or not STATS_PASS:
            return True
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            dec = base64.b64decode(h[6:]).decode("utf-8")
        except Exception:
            return False
        user, _, pw = dec.partition(":")
        return hmac.compare_digest(user, STATS_USER) and hmac.compare_digest(pw, STATS_PASS)

    def _deny(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="FizzDisk stats", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()

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
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/health", "/api/license/health"):
            return self._json(200, {"ok": True})

        # ---- download counter (public) ----
        if path in ("/dl", "/api/license/dl"):
            try:
                ver = (parse_qs(parsed.query).get("ver") or ["unknown"])[0][:64]
            except Exception:
                ver = "unknown"
            ver = ver.strip() or "unknown"
            with LOCK:
                db = _load_dl()
                db[ver] = int(db.get(ver, 0)) + 1
                _save_dl(db)
                count = db[ver]
            return self._json(200, {"ok": True, "version": ver, "count": count})

        # ---- private stats ----
        if path in ("/stats", "/api/license/stats", "/stats.json", "/api/license/stats.json"):
            if not self._auth_ok():
                return self._deny()
            with LOCK:
                db = _load_dl()
            if path.endswith(".json"):
                return self._json(200, {
                    "total": sum(db.values()),
                    "versions": dict(sorted(db.items(),
                                            key=lambda kv: _version_key(kv[0]),
                                            reverse=True)),
                })
            return self._html(200, _stats_html(db))

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
    print("license API on :%d  data=%s  dl=%s  stats_auth=%s" %
          (port, DATA, DOWNLOADS, bool(STATS_USER and STATS_PASS)), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
