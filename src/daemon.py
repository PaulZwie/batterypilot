#!/usr/bin/env python3
"""BatteryPilot daemon (root). Event driven, the charge control backend is probed at runtime."""
import json, os, plistlib, re, select, shutil, signal, subprocess, sys, time

D = "/Library/Application Support/BatteryPilot"
C = f"{D}/cfg"
CFG, ST, HIST = f"{C}/config.json", f"{D}/status.json", f"{D}/history.jsonl"
CAL, RES = f"{D}/calibration.json", f"{D}/result.json"
ORIG = f"{D}/native_orig.json"
PD = "com.apple.smartcharging.topoffprotection"
SMC = os.environ.get("SMC") or shutil.which("smc") or next(
    (x for x in ("/usr/local/libexec/batterypilot/smc", "/usr/local/bin/smc", "/opt/homebrew/bin/smc") if os.access(x, os.X_OK)), "smc")
ACTIVE = ("charge", "hold", "drain", "recharge")
DEFAULT = {"active": "home", "override": None, "calibrate": None, "notify_limit": False,
           "profiles": {"home": {"limit": 65, "sail": 55},
                        "work": {"limit": 80, "sail": 70},
                        "travel": {"limit": 100, "sail": 95}}}
backend = None  # chte, legacy, adapter or none


def run(*a):
    try:
        return subprocess.run(a, capture_output=True, text=True)
    except OSError:  # tool missing, for example no smc on macOS 27
        return subprocess.CompletedProcess(a, 127, "", "not found")


def load(p, default=None):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return default


def save(p, obj, perm=0o644):
    t = p + ".tmp"
    with open(t, "w") as f:
        json.dump(obj, f)
    os.chmod(t, perm)
    os.replace(t, p)


def has(key):
    r = run(SMC, "-k", key, "-r")
    t = (r.stdout + r.stderr).lower()
    return r.returncode == 0 and not any(w in t for w in ("no data", "error", "not found"))


def smc(key, val):
    r = run(SMC, "-k", key, "-w", val)
    return r.returncode == 0 and "error" not in (r.stdout + r.stderr).lower()


def detect():
    """Firmware before 26.7 has CHTE or CH0B/CH0C. Newer firmware dropped them, so we drive the macOS charge limit."""
    if has("CHTE"):
        return "chte"
    if has("CH0B") or has("CH0C"):
        return "legacy"
    return "native"


def dread(key):
    r = run("defaults", "read", PD, key)
    t = r.stdout.strip()
    return int(t) if r.returncode == 0 and t.lstrip("-").isdigit() else None


def native_set(limit):
    """Hold at limit through the macOS charge limit. The firmware enforces it, also in sleep and across reboots."""
    if not os.path.exists(ORIG):
        save(ORIG, {"state": dread("MCLFeatureState"), "limit": dread("mclLimitValue")})
    ok = True
    if limit is None or limit >= 100:
        ok = run("defaults", "write", PD, "MCLFeatureState", "-int", "0").returncode == 0
    else:  # PowerUIAgent only range checks its XPC setter, so values below 80 work when written as root
        ok = run("defaults", "write", PD, "mclLimitValue", "-int", str(limit)).returncode == 0
        ok = run("defaults", "write", PD, "MCLFeatureState", "-int", "1").returncode == 0 and ok
    run("notifyutil", "-p", "com.apple.smartcharging.defaultschanged")
    return ok


def native_restore():
    """Hand back whatever limit the user had before BatteryPilot touched it."""
    o = load(ORIG)
    if o is None:
        return
    for key, val in (("MCLFeatureState", o["state"]), ("mclLimitValue", o["limit"])):
        run(*(("defaults", "delete", PD, key) if val is None else ("defaults", "write", PD, key, "-int", str(val))))
    run("notifyutil", "-p", "com.apple.smartcharging.defaultschanged")
    os.remove(ORIG)


def fw_limit():
    m = re.search(r"chargeSocLimitSoc\s*=\s*(\d+)", run("pmset", "-g", "battlimit").stdout)
    return int(m.group(1)) if m else None


def set_charging(on, lim=None):
    """on=False pauses charging (chte, legacy) or holds at lim (native). on=True lifts the restriction."""
    global backend
    if backend is None:
        backend = detect()
    if backend == "chte":
        ok = smc("CHTE", "00000000" if on else "01000000")
    elif backend == "legacy":
        ok = any([smc(k, "00" if on else "02") for k in ("CH0B", "CH0C")])
    else:
        ok = native_set(None if on else lim)
    if not ok:
        backend = None  # probe again next time
    return ok


def battery():
    b = plistlib.loads(run("ioreg", "-arn", "AppleSmartBattery").stdout.encode())[0]
    cur, mx = b.get("CurrentCapacity", 0), b.get("MaxCapacity", 100) or 100
    amp = b.get("InstantAmperage", 0)
    if amp > 2**63:
        amp -= 2**64
    return dict(pct=cur if mx == 100 else round(100 * cur / mx), plugged=bool(b.get("ExternalConnected")),
                design=b.get("DesignCapacity", 0),
                raw_max=b.get("AppleRawMaxCapacity", mx), raw_cur=b.get("AppleRawCurrentCapacity", cur),
                cycles=b.get("CycleCount", 0), temp=b.get("Temperature", 0) / 100,
                volt=b.get("Voltage", 0), amps=amp)


class Wake:
    """Sleeps until the power state changes, the config folder changes, or the timeout ends."""
    def __init__(self, watch):
        self.kq, self.p = select.kqueue(), None
        self.fd = os.open(watch, os.O_RDONLY)
        self.kq.control([select.kevent(self.fd, select.KQ_FILTER_VNODE, select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                                       select.KQ_NOTE_WRITE)], 0)

    def wait(self, timeout):
        if self.p is None or self.p.poll() is not None:
            self.p = subprocess.Popen(["pmset", "-g", "pslog"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        out = self.p.stdout.fileno()
        for fd in select.select([out, self.kq.fileno()], [], [], timeout)[0]:
            if fd == out:
                if not os.read(out, 65536):
                    time.sleep(min(timeout, 30))  # pslog died, avoid a busy loop
            else:
                self.kq.control(None, 8, 0)


def calibrate(cal, b, dt, on_batt, auto):
    """Stages are charge, hold, drain, recharge, done. Returns (charging allowed, message)."""
    s = cal["stage"]
    if s == "charge":
        if b["pct"] >= 100:
            cal.update(stage="hold", until=time.time() + 3600, before=b["raw_max"])
        return True, "Calibration 1/4, charging to 100%"
    if s == "hold":
        if time.time() > cal["until"]:
            cal.update(stage="drain", mah=0.0, start=None)
        return True, "Calibration 2/4, holding at 100% for an hour"
    if s == "drain":
        if not on_batt:
            return False, ("Calibration 3/4, switching to battery" if auto
                           else "Calibration 3/4, unplug the charger and keep using the Mac")
        if cal["start"] is None:
            cal["start"] = b["pct"]
        cal["mah"] += max(0, -b["amps"]) * min(dt, 60) / 3600  # coulomb counting
        if b["pct"] <= 10:
            cal.update(stage="recharge", end=b["pct"])
        return False, f"Calibration 3/4, draining to 10% (now {b['pct']}%)"
    if b["pct"] >= 100:
        drop = cal["start"] - cal["end"]
        meas = cal["mah"] / (drop / 100) if drop >= 40 else None
        save(RES, {"time": time.time(), "measured_mah": meas, "design_mah": b["design"],
                   "health": meas / b["design"] * 100 if meas and b["design"] else None,
                   "gauge_before": cal["before"], "gauge_after": b["raw_max"], "cycles": b["cycles"]})
        cal["stage"] = "done"
    return True, ("Calibration 4/4, plug the charger back in" if on_batt and not auto
                  else "Calibration 4/4, recharging to 100%")


def log(row):
    try:
        if os.path.getsize(HIST) > 1_000_000:
            with open(HIST) as f:
                keep = f.readlines()[-3000:]
            with open(HIST, "w") as f:
                f.writelines(keep)
    except OSError:
        pass
    with open(HIST, "a") as f:
        f.write(json.dumps(row) + "\n")


def main():
    global backend
    os.makedirs(C, exist_ok=True)
    os.chmod(D, 0o755)
    os.chmod(C, 0o775)
    os.chown(C, 0, 80)
    if sys.argv[1:] == ["probe"]:
        be = detect()
        print("backend:", be, {k: has(k) for k in ("CHTE", "CH0B", "CH0C")})
        if be == "native":
            pct = battery()["pct"]
            print(f"Holding at the current level of {pct}% for up to 70 s to check the macOS charge limit...")
            native_set(pct)
            for _ in range(14):
                time.sleep(5)
                if fw_limit() == pct:
                    break
            print("firmware reports", fw_limit(), "expected", pct)
            native_restore()
        else:
            print("pause ok:", set_charging(False), "| resume ok:", set_charging(True))
        return
    signal.signal(signal.SIGTERM, lambda *_: (native_restore() if backend == "native" else set_charging(True), sys.exit(0)))
    if not os.path.exists(CFG):
        save(CFG, DEFAULT, 0o664)
    wake, cal = Wake(C), load(CAL, {"stage": None})
    allowed = caf = last_key = applied = plug_prev = None
    held = (load(ST) or {}).get("allowed")  # band state survives a restart
    t_apply = 0
    last = time.time()
    next_assert = fail_until = last_write = last_hist = 0
    ok, interval = True, 0
    while True:
        wake.wait(interval)
        now = time.time()
        if now - last < 3:
            time.sleep(3 - (now - last))
            now = time.time()
        dt, last = now - last, now
        cfg = load(CFG) or DEFAULT
        b = battery()
        if b["plugged"] != plug_prev:  # charger pulled or replugged, the hardware may have reset, so apply again
            next_assert, plug_prev = 0, b["plugged"]
        req = cfg.get("calibrate")
        if req == "start" and cal.get("stage") in (None, "done"):
            cal = {"stage": "charge"}
        elif req == "cancel":
            cal = {"stage": None}
            save(CAL, cal)
        if req:
            cfg["calibrate"] = None
            save(CFG, cfg, 0o664)
        pname = cfg.get("active")
        p = cfg["profiles"].get(pname) if pname else None
        if p is None:  # no profile selected, use the custom limit from the slider
            p, pname = cfg.get("manual") or DEFAULT["profiles"]["home"], "custom"
        lim = min(100, max(20, int(p["limit"])))
        sail = min(lim - 1, max(5, int(p["sail"])))
        if backend is None:
            backend = detect()
        native, auto = backend == "native", False
        on_batt = not b["plugged"]
        stage, msg = cal.get("stage"), ""
        if stage in ACTIVE:
            want, msg = calibrate(cal, b, dt, on_batt, auto)
            save(CAL, cal)
            stage = cal["stage"]
            want = want or native  # keep the macOS limit off while calibrating
        elif cfg.get("override") == "full":
            want, msg = True, "Charging to 100% once"
            if b["pct"] >= 100:
                cfg["override"] = None
                save(CFG, cfg, 0o664)
        elif native:
            want = lim >= 100  # hold at lim, the firmware decides when to top up
        elif lim >= 100 or b["pct"] <= max(sail, 15 if auto else 10):
            want = True
        elif b["pct"] >= lim:
            want = False
        else:  # inside the sailing band, keep doing what we did
            want = True if held is None else held
        held = want
        sig = (want, lim if native and not want else None)
        if now >= fail_until and (sig != applied or now >= next_assert):
            if sig != applied:
                t_apply = now
            ok = set_charging(want, lim)  # re-assert now and then, the hardware can reset on sleep
            applied = sig
            next_assert = now + 300
            if ok:
                allowed = want
            else:
                fail_until = now + 300
        keep = stage == "drain"  # stop idle sleep so the integration has no gaps
        if keep and not caf:
            caf = subprocess.Popen(["caffeinate", "-i"])
        elif not keep and caf:
            caf.terminate()
            caf = None
        if native and not want and now - t_apply > 150 and not msg:  # soft check that macOS accepted the limit
            fw = fw_limit()
            if fw != lim:
                msg = f"macOS reports {fw}%, expected {lim}%" if fw else f"macOS limit not active yet, expected {lim}%"
        near = min(abs(b["pct"] - lim), abs(b["pct"] - sail)) <= 3
        interval = 30 if (stage in ACTIVE or near or cfg.get("override")) else 180
        st = dict(b, allowed=allowed is not False, profile=pname, limit=lim, sail=sail, msg=msg,
                  stage=stage, calibrating=stage in ACTIVE, smc_ok=ok, mode=backend,
                  cut=backend == "adapter" and allowed is False,
                  health=b["raw_max"] / b["design"] * 100 if b["design"] else None)
        key = tuple(st[k] for k in ("pct", "plugged", "allowed", "profile", "limit", "sail", "msg", "stage", "smc_ok", "cut"))
        if key != last_key or now - last_write > 600:  # write only on change, spares the SSD
            save(ST, dict(st, t=now))
            last_key, last_write = key, now
        if now - last_hist >= 900:
            last_hist = now
            log(dict({k: st[k] for k in ("pct", "raw_cur", "raw_max", "cycles", "temp", "volt", "amps", "plugged", "allowed")}, t=int(now)))


if __name__ == "__main__":
    main()
