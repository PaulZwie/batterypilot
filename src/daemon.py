#!/usr/bin/env python3
"""BatteryPilot daemon (root). Event driven, the charge control backend is probed at runtime."""
import datetime, json, os, plistlib, re, select, shutil, signal, subprocess, sys, time

D = "/Library/Application Support/BatteryPilot"
C = f"{D}/cfg"
CFG, ST, HIST = f"{C}/config.json", f"{D}/status.json", f"{D}/history.jsonl"
CAL, RES = f"{D}/calibration.json", f"{D}/result.json"
ORIG = f"{D}/native_orig.json"
SCHED = f"{D}/schedule_state.json"  # last applied schedule event and the ready-by plan that is charging
MIN_PER_PCT, READY_BUFFER, READY_HOLD = 1.0, 15 * 60, 2 * 3600  # minutes to charge one percent, safety margin, hold at 100% after the deadline
PD = "com.apple.smartcharging.topoffprotection"
SMC = os.environ.get("SMC") or shutil.which("smc") or next(
    (x for x in ("/usr/local/libexec/batterypilot/smc", "/usr/local/bin/smc", "/opt/homebrew/bin/smc") if os.access(x, os.X_OK)), "smc")
ACTIVE = ("charge", "hold", "drain", "recharge")
DEFAULT = {"active": "home", "override": None, "calibrate": None, "notify_limit": False, "schedule": [],
           "profiles": {"home": {"limit": 65, "sail": 55},
                        "work": {"limit": 80, "sail": 70},
                        "travel": {"limit": 100, "sail": 95}}}
backend = None  # chte, legacy, adapter or none
rel_alt = False  # second way to lift the macOS limit, used when the first one did not take
nat = None  # on Macs with SMC control: "held" or "lifted", what we last did to the macOS limit
fw_ok = None  # does this Mac offer the macOS charge limit at all


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
    # The temp file lives in D, which only root can write. The cfg folder is group-writable, so a temp file created next
    # to config.json there could be swapped for a symlink by another process and make root overwrite any file.
    t = os.path.join(D, "." + os.path.basename(p) + ".tmp")
    with open(t, "w") as f:
        json.dump(obj, f)
    os.chmod(t, perm)
    os.replace(t, p)


def target(cfg):
    """Percent of a running one-time charge, or None. The config is writable by users, so only a clean number counts.
    Older versions stored the word full, which means 100."""
    ov = cfg.get("override")
    if ov == "full":
        return 100
    if isinstance(ov, int) and not isinstance(ov, bool):
        return min(100, max(20, ov))
    return None


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
    if limit is None or limit >= 100:  # lift the limit: raise the stored value to 100 first, then switch the feature off
        ok = run("defaults", "write", PD, "mclLimitValue", "-int", "100").returncode == 0
        ok = run("defaults", "write", PD, "MCLFeatureState", "-int", "1" if rel_alt else "0").returncode == 0 and ok
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


def fw_limits():
    """Every charge limit the firmware currently holds. Two owners can each hold one, and the lowest one wins."""
    return [int(v) for v in re.findall(r"chargeSocLimitSoc\s*=\s*(\d+)", run("pmset", "-g", "battlimit").stdout)]


def macos_limit_ok():
    """The macOS charge limit is the only control on newer firmware. Older Macs can have it too, if pmset knows about it."""
    global fw_ok
    if backend == "native":
        return True
    if fw_ok is None:
        r = run("pmset", "-g", "battlimit")
        fw_ok = r.returncode == 0 and not any(w in (r.stdout + r.stderr).lower() for w in ("invalid", "unknown", "usage"))
    return fw_ok


def set_charging(on, lim=None, strat="band"):
    """on=False pauses charging (chte, legacy) or holds at lim (native). on=True lifts the restriction.
    strat=macos lets the macOS charge limit do the work, which also tops up by itself. On a Mac with SMC control the
    SMC then stays out of the way. strat=band drives the pause itself, through the SMC or, on native, through the macOS limit."""
    global backend, nat
    if backend is None:
        backend = detect()
    if backend == "native":
        ok = native_set(None if on else lim)
    else:
        free = on or strat == "macos"
        if backend == "chte":
            ok = smc("CHTE", "00000000" if free else "01000000")
        else:
            ok = any([smc(k, "00" if free else "02") for k in ("CH0B", "CH0C")])
        if strat == "macos":
            ok = native_set(None if on else lim) and ok
            nat = "lifted" if on else "held"
        elif nat != "lifted" and os.path.exists(ORIG):  # leave no macOS limit behind that would block the band
            lifted = native_set(None)
            ok, nat = ok and lifted, "lifted" if lifted else nat
    if not ok:
        backend = None  # probe again next time
    return ok


def pick(b, key, default=0):
    """Some Macs report capacity values inside BatteryData instead of at the top level."""
    v = b.get(key)
    if v is None:
        v = (b.get("BatteryData") or {}).get(key)
    return default if v is None else v


def battery():
    b = plistlib.loads(run("ioreg", "-arn", "AppleSmartBattery").stdout.encode())[0]
    cur, mx = b.get("CurrentCapacity", 0), b.get("MaxCapacity", 100) or 100
    amp = b.get("InstantAmperage", 0)
    if amp > 2**63:
        amp -= 2**64
    t = pick(b, "Temperature", None)  # not every Mac reports a temperature
    return dict(pct=cur if mx == 100 else round(100 * cur / mx), plugged=bool(b.get("ExternalConnected")),
                design=pick(b, "DesignCapacity"),
                raw_max=pick(b, "AppleRawMaxCapacity"), raw_cur=pick(b, "AppleRawCurrentCapacity", cur),
                cycles=pick(b, "CycleCount"), temp=None if t is None else t / 100,
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


def selection(cfg):
    """What the person picked as limit: the active profile, or the custom limit."""
    return json.dumps([cfg.get("active"), cfg.get("manual"), cfg["profiles"].get(cfg.get("active"))], sort_keys=True)


def hm(t, now):
    """Clock time of an epoch, with the weekday when it is not today."""
    same = time.strftime("%Y%j", time.localtime(t)) == time.strftime("%Y%j", time.localtime(now))
    return time.strftime("%H:%M" if same else "%a %H:%M", time.localtime(t))


def fires(rule, now, back=0, ahead=0):
    """Epochs at which a schedule rule triggers, from `back` days before today to `ahead` days after. Days are 0 = Monday."""
    try:
        h, m = (int(x) for x in rule["time"].split(":"))
        days = rule.get("days") or range(7)
        today = datetime.date.fromtimestamp(now)
        out = []
        for k in range(-back, ahead + 1):
            d = today + datetime.timedelta(days=k)
            if d.weekday() in days:
                out.append(int(datetime.datetime.combine(d, datetime.time(h, m)).timestamp()))
        return out
    except Exception:  # a broken rule never stops the daemon
        return []


def lead(pct):
    """Seconds needed to charge from pct to 100%, plus a safety margin."""
    return max(0, 100 - pct) * MIN_PER_PCT * 60 + READY_BUFFER


def ready_times(cfg, now):
    """Deadlines of the one-time and the recurring ready-by plans around now."""
    one, out = cfg.get("ready_by"), []
    if isinstance(one, dict) and isinstance(one.get("at"), (int, float)):
        out.append(int(one["at"]))
    for r in cfg.get("schedule") or []:
        if r.get("action") == "ready":
            out += fires(r, now, back=1, ahead=1)
    return out


def ready_active(cfg, pct, now, started):
    """Deadline of the ready-by plan that is charging now, or None. A plan starts early enough to be full at the deadline,
    keeps going once started, and holds 100% for READY_HOLD afterwards."""
    for t in sorted(ready_times(cfg, now)):
        if t != cfg.get("ready_skip") and now <= t + READY_HOLD and (t == started or t - lead(pct) <= now):
            return t
    return None


def due_profile(cfg, now, since):
    """The newest profile switch that triggered after `since` and up to now, as (epoch, rule), or None."""
    best = None
    for r in cfg.get("schedule") or []:
        if r.get("action") == "profile" and r.get("profile") in cfg["profiles"]:
            for t in fires(r, now, back=1):
                if since < t <= now and (best is None or t > best[0]):
                    best = (t, r)
    return best


def plan(cfg, now):
    """The next few events as [epoch, kind, profile], soonest first. Kind is profile or ready."""
    ev = [[t, r.get("action"), r.get("profile")] for r in cfg.get("schedule") or [] for t in fires(r, now, ahead=7) if t > now]
    one = cfg.get("ready_by")
    if isinstance(one, dict) and isinstance(one.get("at"), (int, float)) and one["at"] > now:
        ev.append([int(one["at"]), "ready", None])
    return sorted(ev, key=lambda e: e[0])[:4]


def main():
    global backend, rel_alt
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
    signal.signal(signal.SIGTERM, lambda *_: (None if backend == "native" else set_charging(True), native_restore(), sys.exit(0)))
    if not os.path.exists(CFG):
        save(CFG, DEFAULT, 0o664)
    wake, cal = Wake(C), load(CAL, {"stage": None})
    sst = load(SCHED, {})
    sched_last = max(sst.get("last", time.time()), time.time() - 3600)  # after a restart only recent events still count
    allowed = caf = last_key = applied = plug_prev = pick_prev = None
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
        pick = selection(cfg)
        tgt = target(cfg)
        if pick_prev is not None and pick != pick_prev and (tgt or cfg.get("pause_until")):
            cfg["override"] = cfg["pause_until"] = None  # choosing a limit or profile ends a one-time charge or a pause
            save(CFG, cfg, 0o664)
            tgt = None
        rules_sig = json.dumps(cfg.get("schedule") or [], sort_keys=True)
        if sst.get("sig") != rules_sig:  # rules changed, only events from now on count
            sst["sig"], sched_last = rules_sig, now
            save(SCHED, sst)
        due = due_profile(cfg, now, sched_last)
        sched_last = now
        if due:
            cfg["active"] = due[1]["profile"]
            save(CFG, cfg, 0o664)
            sst.update(applied=int(now), last=int(now))
            save(SCHED, sst)
            pick = selection(cfg)  # a scheduled switch is not a manual choice, it must not end a pause
        pick_prev = pick
        pu = cfg.get("pause_until")
        if pu and now >= pu:
            cfg["pause_until"] = pu = None
            save(CFG, cfg, 0o664)
        one = cfg.get("ready_by")
        if one is not None and not (isinstance(one, dict) and now <= one.get("at", 0) + READY_HOLD):
            cfg["ready_by"] = None  # a one-time plan is used up
            save(CFG, cfg, 0o664)
        rdy = ready_active(cfg, b["pct"], now, sst.get("ready"))
        if rdy != sst.get("ready"):
            sst["ready"] = rdy
            save(SCHED, sst)
        pname = cfg.get("active")
        p = cfg["profiles"].get(pname) if pname else None
        if p is None:  # no profile selected, use the custom limit from the slider
            p, pname = cfg.get("manual") or DEFAULT["profiles"]["home"], "custom"
        if pu or rdy:  # a pause or a ready-by plan lifts the limit for now
            p, pname = {"limit": 100, "sail": 95}, "paused" if pu else "ready"
        lim = min(100, max(20, int(p["limit"])))
        sail = min(lim - 1, max(5, int(p["sail"])))
        if backend is None:
            backend = detect()
        native, auto = backend == "native", False
        strat = p.get("strategy") or ("macos" if native else "band")  # macos: one limit, macOS tops up. band: hold at limit, resume at sail
        if strat == "macos" and not macos_limit_ok():
            strat = "band"
        on_batt = not b["plugged"]
        stage, msg, warn = cal.get("stage"), "", ""
        if tgt and stage not in ACTIVE and b["pct"] >= tgt:  # target reached, the profile takes over again right now
            cfg["override"] = tgt = None
            save(CFG, cfg, 0o664)
        if stage in ACTIVE:
            strat = "band"  # calibration needs charging fully on or off
            want, msg = calibrate(cal, b, dt, on_batt, auto)
            save(CAL, cal)
            stage = cal["stage"]
            want = want or native  # keep the macOS limit off while calibrating
        elif tgt:
            want, msg = True, f"Charging to {tgt}% once"
        elif strat == "macos":
            want = lim >= 100  # hold at lim, the firmware decides when to top up
        elif lim >= 100 or b["pct"] <= max(sail, 15 if auto else 10):
            want = True
        elif b["pct"] >= lim:
            want = False
        else:  # inside the sailing band, keep doing what we did
            want = True if held is None else held
        if not msg and pu:
            msg = f"Limit paused until {hm(pu, now)}"
        elif not msg and rdy:
            msg = f"Charging to be ready by {hm(rdy, now)}"
        held = want
        hold = lim
        if native and strat == "band" and not want and b["pct"] < lim:
            hold = max(20, b["pct"])  # macOS has no pause, so between the sliders it holds at the current level
        sig = (want, hold if not want and (native or strat == "macos") else None, strat)
        if now >= fail_until and (sig != applied or now >= next_assert):
            if sig != applied:
                t_apply = now
            ok = set_charging(want, hold, strat)  # re-assert now and then, the hardware can reset on sleep
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
        if (native or strat == "macos") and now - t_apply > (30 if want else 150):  # soft check that macOS accepted what we asked for
            vals = fw_limits()
            if want and any(v < 100 for v in vals):  # we asked for charging but the firmware still holds a limit
                if not rel_alt:
                    rel_alt, applied = True, None  # try the other way to lift the limit once
                else:
                    warn = msg = f"macOS still holds the charge limit at {min(vals)}%, charging to 100% is blocked"
            elif not want and not msg:
                fw = fw_limit()
                if fw != hold:
                    msg = f"macOS reports {fw}%, expected {hold}%" if fw else f"macOS limit not active yet, expected {hold}%"
        near = min(abs(b["pct"] - lim), abs(b["pct"] - sail)) <= 3
        interval = 30 if (stage in ACTIVE or near or tgt) else 180
        if pu:
            interval = min(interval, max(2, pu - now))  # wake when the pause ends
        if pu or rdy or cfg.get("schedule") or cfg.get("ready_by"):
            interval = min(interval, 60)  # check the clock about once a minute
        st = dict(b, allowed=allowed is not False, profile=pname, limit=lim, sail=sail, msg=msg,
                  stage=stage, strategy=strat, macos_ok=macos_limit_ok(), calibrating=stage in ACTIVE, smc_ok=ok, mode=backend, override=tgt, warn=warn,
                  pause_until=pu, ready=rdy, lead=int(lead(b["pct"])), plan=plan(cfg, now), sched_t=sst.get("applied", 0),
                  cut=backend == "adapter" and allowed is False,
                  health=b["raw_max"] / b["design"] * 100 if b["design"] and b["raw_max"] else None)
        key = tuple(st[k] for k in ("pct", "plugged", "allowed", "profile", "limit", "sail", "msg", "stage", "smc_ok", "cut", "override", "warn",
                                         "pause_until", "ready", "plan", "sched_t", "strategy"))
        if key != last_key or now - last_write > 600:  # write only on change, spares the SSD
            save(ST, dict(st, t=now))
            last_key, last_write = key, now
        if now - last_hist >= 900:
            last_hist = now
            log(dict({k: st[k] for k in ("pct", "raw_cur", "raw_max", "cycles", "temp", "volt", "amps", "plugged", "allowed")}, t=int(now)))


if __name__ == "__main__":
    main()
