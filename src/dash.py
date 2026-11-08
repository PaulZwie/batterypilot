#!/usr/bin/env python3
"""Local server behind the BatteryPilot popover and overview window. Localhost only, guarded by a random token."""
import json, os, plistlib, secrets, subprocess, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

D = "/Library/Application Support/BatteryPilot"
C = f"{D}/cfg"
CFG, ST, RES, HIST = f"{C}/config.json", f"{D}/status.json", f"{D}/result.json", f"{D}/history.jsonl"
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")
TOKEN, HOOKS, _srv, _live = secrets.token_urlsafe(12), {}, None, (0, {})


def rd(p):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None


def update(fn):
    c = rd(CFG) or {}
    fn(c)
    t = f"{C}/.cfg.{os.getpid()}"
    with open(t, "w") as f:
        json.dump(c, f)
    os.replace(t, CFG)


def history(span):
    rows, cut = [], time.time() - span
    try:
        with open(HIST) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r["t"] >= cut:
                    rows.append(r)
    except OSError:
        pass
    return rows[::max(1, len(rows) // 300)]


def live():
    """Fresh numbers straight from the battery, cached for a few seconds. Power telemetry keys differ per Mac, so all optional."""
    global _live
    if time.time() - _live[0] < 3:
        return _live[1]
    try:
        b = plistlib.loads(subprocess.run(["ioreg", "-arn", "AppleSmartBattery"], capture_output=True).stdout)[0]
    except Exception:
        return {}
    amp = b.get("InstantAmperage", 0)
    if amp > 2**63:
        amp -= 2**64
    t, ad = b.get("PowerTelemetryData") or {}, b.get("AdapterDetails") or {}

    def watts(v):  # telemetry is in milliwatts, ignore anything implausible
        return round(v / 1000, 1) if isinstance(v, (int, float)) and 0 < v / 1000 < 400 else None
    data = {"temp": b.get("Temperature", 0) / 100, "batt_w": round(b.get("Voltage", 0) * amp / 1e6, 1),
            "cycles": b.get("CycleCount", 0), "raw_max": b.get("AppleRawMaxCapacity", 0), "design": b.get("DesignCapacity", 0),
            "sys_w": watts(t.get("SystemLoad")), "adapter_w": ad.get("Watts") if isinstance(ad.get("Watts"), int) else None}
    _live = (time.time(), data)
    return data


def do(a):
    act = a.get("do")
    if act == "profile":
        update(lambda c: c.update(active=a["name"]) if a["name"] in c["profiles"] else None)
    elif act == "set":
        name, lim, sail = str(a["name"]).strip()[:20], int(a["limit"]), int(a["sail"])
        assert name and 20 <= lim <= 100 and 5 <= sail < lim
        update(lambda c: (c["profiles"].__setitem__(name, {"limit": lim, "sail": sail}), c.update(active=name)))
    elif act == "limit":  # custom limit, no profile needed
        lim, sail = int(a["limit"]), int(a["sail"])
        assert 20 <= lim <= 100 and 5 <= sail < lim
        update(lambda c: c.update(manual={"limit": lim, "sail": sail}, active=None))
    elif act == "full":
        update(lambda c: c.update(override="full"))
    elif act == "calibrate" and a.get("what") in ("start", "cancel"):
        update(lambda c: c.update(calibrate=a["what"]))
    elif act == "notify":
        update(lambda c: c.update(notify_limit=bool(a["on"])))
    elif act == "overview" and "overview" in HOOKS:
        HOOKS["overview"]()
    else:
        raise ValueError(act)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body, ctype="application/json"):
        body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, q = self.path.partition("?")
        if not path.startswith(f"/{TOKEN}/"):
            return self.reply(404, b"", "text/plain")
        if path.endswith("/api/state"):
            try:
                span = int(dict(kv.split("=") for kv in q.split("&") if "=" in kv).get("span", 86400))
            except ValueError:
                span = 86400
            return self.reply(200, {"status": rd(ST), "config": rd(CFG), "result": rd(RES), "live": live(), "history": history(span)})
        with open(PAGE, "rb") as f:
            self.reply(200, f.read(), "text/html; charset=utf-8")

    def do_POST(self):
        if not self.path.startswith(f"/{TOKEN}/api/do"):
            return self.reply(404, b"", "text/plain")
        try:
            do(json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)))))
            self.reply(200, {"ok": True})
        except Exception:
            self.reply(400, {"ok": False})


def start():
    """Starts the server once and returns the page URL."""
    global _srv
    if _srv is None:
        _srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=_srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{_srv.server_address[1]}/{TOKEN}/"
