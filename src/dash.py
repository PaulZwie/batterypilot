#!/usr/bin/env python3
"""Local server behind the BatteryPilot popover and overview window. Localhost only, guarded by a random token."""
import datetime, json, os, plistlib, secrets, subprocess, threading, time
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


def pick(b, key, default=0):
    """Some Macs report capacity values inside BatteryData instead of at the top level."""
    v = b.get(key)
    if v is None:
        v = (b.get("BatteryData") or {}).get(key)
    return default if v is None else v


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
    tp = pick(b, "Temperature", None)
    data = {"temp": None if tp is None else tp / 100, "batt_w": round(b.get("Voltage", 0) * amp / 1e6, 1),
            "cycles": pick(b, "CycleCount"), "raw_max": pick(b, "AppleRawMaxCapacity"), "design": pick(b, "DesignCapacity"),
            "sys_w": watts(t.get("SystemLoad")), "adapter_w": ad.get("Watts") if isinstance(ad.get("Watts"), int) else None}
    _live = (time.time(), data)
    return data


def next_at(hhmm):
    """Epoch of the next time the clock shows HH:MM, today or tomorrow."""
    h, m = (int(x) for x in str(hhmm).split(":"))
    assert 0 <= h < 24 and 0 <= m < 60
    t = datetime.datetime.combine(datetime.date.today(), datetime.time(h, m))
    if t.timestamp() <= time.time():
        t += datetime.timedelta(days=1)
    return int(t.timestamp())


def clean_rules(raw, profiles):
    """Checks the schedule sent by the dashboard and keeps only valid fields."""
    out = []
    for r in raw[:20]:
        h, m = (int(x) for x in str(r["time"]).split(":"))
        assert 0 <= h < 24 and 0 <= m < 60
        days = sorted({int(d) for d in r.get("days") or range(7)})
        assert all(0 <= d <= 6 for d in days)
        rule = {"action": r["action"], "time": f"{h:02d}:{m:02d}", "days": days}
        if r["action"] == "profile":
            assert r["profile"] in profiles
            rule["profile"] = r["profile"]
        else:
            assert r["action"] == "ready"
        out.append(rule)
    return out


def strat(a):
    """Optional top-up strategy sent by the dashboard: band (two sliders) or macos (macOS tops up by itself)."""
    return {"strategy": a["strategy"]} if a.get("strategy") in ("band", "macos") else {}


def do(a):
    act = a.get("do")
    if act == "profile":
        update(lambda c: c.update(active=a["name"]) if a["name"] in c["profiles"] else None)
    elif act == "set":
        name, lim, sail = str(a["name"]).strip()[:20], int(a["limit"]), int(a["sail"])
        assert name and 20 <= lim <= 100 and 5 <= sail < lim
        update(lambda c: (c["profiles"].__setitem__(name, dict({"limit": lim, "sail": sail}, **strat(a))), c.update(active=name)))
    elif act == "limit":  # custom limit, no profile needed
        lim, sail = int(a["limit"]), int(a["sail"])
        assert 20 <= lim <= 100 and 5 <= sail < lim

        def f(c):
            keep = strat(a) or {k: v for k, v in (c.get("manual") or {}).items() if k == "strategy"}
            c.update(manual=dict({"limit": lim, "sail": sail}, **keep), active=None)
        update(f)
    elif act == "strategy" and a.get("value") in ("band", "macos"):  # applies to the active profile, or the custom limit
        def f(c):
            t = c["profiles"].get(c.get("active")) if c.get("active") else None
            if t is None:
                t = c.setdefault("manual", {"limit": 80, "sail": 70})
            t["strategy"] = a["value"]
        update(f)
    elif act == "full":
        update(lambda c: c.update(override="full"))
    elif act == "calibrate" and a.get("what") in ("start", "cancel"):
        update(lambda c: c.update(calibrate=a["what"]))
    elif act == "pause":  # minutes, 0 resumes the limit
        m = int(a["minutes"])
        assert 0 <= m <= 2880
        update(lambda c: c.update(pause_until=int(time.time() + m * 60) if m else None))
    elif act == "ready":  # one-time ready-by plan, no time removes it
        at = next_at(a["time"]) if a.get("time") else None
        update(lambda c: c.update(ready_by={"at": at} if at else None, ready_skip=None))
    elif act == "ready_cancel":  # skip the plan that is charging right now
        t = int(a["at"])
        update(lambda c: c.update(ready_skip=t, ready_by=None if (c.get("ready_by") or {}).get("at") == t else c.get("ready_by")))
    elif act == "schedule":
        rules = clean_rules(a["rules"], (rd(CFG) or {}).get("profiles", {}))
        update(lambda c: c.update(schedule=rules))
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
