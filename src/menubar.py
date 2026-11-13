#!/usr/bin/env python3
"""BatteryPilot menu bar app. Reads status.json, writes config.json, posts notifications."""
import datetime, json, os, subprocess, time
import rumps
from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
import dash, icons

D = "/Library/Application Support/BatteryPilot"
C = f"{D}/cfg"
CFG, ST, RES = f"{C}/config.json", f"{D}/status.json", f"{D}/result.json"
ACTIVE = ("charge", "hold", "drain", "recharge")


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


def front():
    """Without a Dock icon macOS does not raise our dialogs by itself."""
    NSApplication.sharedApplication().activateIgnoringOtherApps_(True)


def notify(msg):
    """The text goes in as an argument, so quotes in a message cannot break the script."""
    subprocess.Popen(["osascript", "-e", "on run argv", "-e", 'display notification (item 1 of argv) with title "BatteryPilot"',
                      "-e", "end run", msg])


def clock(t):
    """Clock time of an epoch, with the weekday when it is not today."""
    same = datetime.date.fromtimestamp(t) == datetime.date.today()
    return time.strftime("%H:%M" if same else "%a %H:%M", time.localtime(t))


def next_at(hhmm):
    h, m = (int(x) for x in hhmm.strip().split(":"))
    assert 0 <= h < 24 and 0 <= m < 60
    t = datetime.datetime.combine(datetime.date.today(), datetime.time(h, m))
    if t.timestamp() <= time.time():
        t += datetime.timedelta(days=1)
    return int(t.timestamp())


def macos(s):
    """True when macOS tops the battery up by itself (one limit), false for the two-slider band."""
    return s.get("strategy", "macos" if s["mode"] == "native" else "band") == "macos"


def once(s):
    """Percent of a running one-time charge, or None. Status files of older versions say full for 100."""
    o = s.get("override")
    return 100 if o == "full" else o if isinstance(o, int) and not isinstance(o, bool) else None


def above_limit(s):
    """Plugged in, charging held off, and the battery is higher than the limit, so it will drain down to it."""
    return bool(s["plugged"] and not s["allowed"] and not s["calibrating"] and s["pct"] > s["limit"])


def limit_events(p, s):
    """Messages about charging between two status snapshots, p before and s now."""
    lim, out = s["limit"], []
    ts, tp = once(s), once(p)
    start = ts is not None and ts != tp
    end = tp is not None and ts is None
    pause_on, pause_off = bool(s.get("pause_until")) and not p.get("pause_until"), bool(p.get("pause_until")) and not s.get("pause_until")
    ready_on, ready_off = bool(s.get("ready")) and not p.get("ready"), bool(p.get("ready")) and not s.get("ready")
    if start:
        out.append(f"Charging to {ts}% now." if s["plugged"] else f"Will charge to {ts}% as soon as the charger is plugged in.")
    if end:
        out.append(f"Charged to {tp}%." if s["pct"] >= tp - 1 else f"Charge to {tp}% cancelled, back to your profile.")
    if pause_on:
        out.append(f"Limit paused until {clock(s['pause_until'])}.")
    if pause_off:
        out.append(f"Limit pause is over, the limit of {lim}% is active again.")
    if ready_on:
        out.append(f"Charging now to be ready by {clock(s['ready'])}.")
    if ready_off:
        out.append("Ready-by charging is over.")
    if s.get("sched_t") and s["sched_t"] != p.get("sched_t") and s["profile"] not in ("paused", "ready"):
        out.append(f"Schedule: switched to {s['profile']}, limit {lim}%.")
    limit_same = s["limit"] == p["limit"]
    if above_limit(s) and not above_limit(p):
        out.append(f"Battery is above your limit of {lim}%, it will drain down to {lim}% before holding." if macos(s)
                   else f"Battery is above your limit of {lim}%, charging stays off until it falls to {s['sail']}%.")
    elif above_limit(p) and s["plugged"] and not s["allowed"] and s["pct"] <= lim and not s["calibrating"] and limit_same:
        out.append(f"Down to the limit of {lim}%, holding here.")
    elif not (end or pause_off or ready_off) and ((p["allowed"] and not s["allowed"])
                                                  or (macos(s) and p["pct"] < lim <= s["pct"])):
        out.append(f"Limit of {lim}% reached.")
    if not p["allowed"] and s["allowed"] and s["plugged"] and not (start or pause_on or ready_on) \
            and ts is None and not s.get("pause_until") and not s.get("ready"):
        out.append(f"Charging resumed, limit is {lim}%.")
    return out


class App(rumps.App):
    def __init__(self):
        super().__init__("BatteryPilot", title="", icon=icons.icon("battery", 50), template=True, quit_button=None)
        self.head, self.sub = rumps.MenuItem("BatteryPilot"), rumps.MenuItem("")
        self.prof, self.cal = rumps.MenuItem("Profile"), rumps.MenuItem("Calibration")
        self.pause = rumps.MenuItem("Pause limit")
        self.pause.update([rumps.MenuItem(t, callback=lambda _, m=m: self.pause_for(m))
                           for t, m in (("for 30 minutes", 30), ("for 1 hour", 60), ("for 2 hours", 120), ("for 4 hours", 240))]
                          + [rumps.MenuItem("Resume limit now", callback=lambda _: self.pause_for(0))])
        self.once = rumps.MenuItem("Charge once to")
        self.once_x = rumps.MenuItem("Cancel, back to profile")  # greyed out until a one-time charge is running
        self.once.update([rumps.MenuItem(f"{v}%", callback=lambda _, v=v: self.charge_to(v)) for v in (80, 90, 100)]
                         + [rumps.MenuItem("Other…", callback=self.charge_other), None, self.once_x])
        self.once_x_on = False
        self.nl = rumps.MenuItem("Notify when limit is reached",
                                 callback=lambda _: update(lambda c: c.update(notify_limit=not c.get("notify_limit"))))
        self.cal.update([rumps.MenuItem("Start full calibration", callback=self.cal_start),
                         rumps.MenuItem("Cancel calibration", callback=lambda _: update(lambda c: c.update(calibrate="cancel"))),
                         rumps.MenuItem("Last result", callback=self.cal_result)])
        self.menu = [self.head, self.sub, None, self.prof,
                     rumps.MenuItem("Edit or add profile…", callback=self.edit),
                     rumps.MenuItem("Set custom limit…", callback=self.custom),
                     self.once,
                     self.pause, rumps.MenuItem("Ready by…", callback=self.ready_by),
                     rumps.MenuItem("Cancel ready-by", callback=self.ready_cancel),
                     self.nl, None, self.cal, rumps.MenuItem("Battery statistics…", callback=self.stats),
                     rumps.MenuItem("Open dashboard…", callback=lambda _: subprocess.Popen(["open", dash.start()])),
                     None, rumps.MenuItem("Quit", callback=self.quit_app)]
        self.strat = rumps.MenuItem("Let macOS top up (one limit)", callback=self.toggle_strategy)
        self.menu.insert_after("Set custom limit…", self.strat)
        self.names, self.s, self.mtime, self.nag_t = None, None, 0, 0
        try:
            self.tick(None)
        except Exception:
            pass

    def quit_app(self, _):
        """Unload the launch agent first, so launchd does not start the app again. The daemon keeps enforcing the limit."""
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/com.batterypilot.menu"])
        rumps.quit_application()  # reached when started by hand instead of by launchd

    def toggle_strategy(self, _):
        """Switch the active profile (or the custom limit) between the two sliders and the macOS top-up."""
        s = rd(ST) or {}
        if not s:
            return
        new = "band" if macos(s) else "macos"
        if new == "macos" and not s.get("macos_ok", True):
            return notify("This Mac does not offer the macOS charge limit.")

        def f(c):
            t = c["profiles"].get(c.get("active")) if c.get("active") else None
            if t is None:
                t = c.setdefault("manual", {"limit": 80, "sail": 70})
            t["strategy"] = new
        update(f)

    def charge_to(self, pct):
        """One-time charge to pct. The daemon ends it when the level is reached and the profile applies again."""
        s = rd(ST) or {}
        if not 20 <= pct <= 100:
            return notify("Pick a value between 20 and 100.")
        if pct <= s.get("pct", 0):
            return notify(f"The battery is already at {s['pct']}%, pick a higher value.")
        if pct <= s.get("limit", 0):
            return notify(f"Charging already goes up to {s['limit']}% right now, pick a higher value.")
        update(lambda c: c.update(override=pct))

    def charge_other(self, _):
        front()
        s = rd(ST) or {}
        start = min(100, max(s.get("limit", 80), s.get("pct", 0)) + 10)
        w = rumps.Window("Percent to charge to, for example 85. After that your profile takes over again.",
                         "Charge once to", str(start), "Charge", "Cancel", dimensions=(260, 24))
        r = w.run()
        try:
            assert r.clicked
            pct = int(r.text.strip().rstrip("%"))
        except Exception:
            return
        self.charge_to(pct)

    def charge_cancel(self, _):
        """Drop the one-time charge, the profile applies again right away."""
        update(lambda c: c.update(override=None))

    def pause_for(self, minutes):
        until = int(time.time() + minutes * 60) if minutes else None
        update(lambda c: c.update(pause_until=until))

    def ready_by(self, _):
        front()
        w = rumps.Window("Time as HH:MM, for example 08:00. The limit stays until shortly before, then the battery charges "
                         "to 100% so it is full by then. For repeating plans use the dashboard schedule.",
                         "Ready by", "08:00", "Set", "Cancel", dimensions=(260, 24))
        r = w.run()
        try:
            assert r.clicked
            at = next_at(r.text)
        except Exception:
            return
        update(lambda c: c.update(ready_by={"at": at}, ready_skip=None))

    def ready_cancel(self, _):
        t = (rd(ST) or {}).get("ready")
        update(lambda c: c.update(ready_by=None, ready_skip=t if t else c.get("ready_skip")))

    def build_profiles(self, c):
        self.names = list(c["profiles"])
        self.prof.clear()
        for n in self.names:
            self.prof.add(rumps.MenuItem(n, callback=lambda _, n=n: update(lambda c: c.update(active=n))))

    @rumps.timer(3)
    def tick(self, _):
        try:
            m = os.stat(ST).st_mtime  # the daemon only rewrites the file on change, so most ticks stop here
        except OSError:
            return
        if m != self.mtime:
            self.mtime = m
            s, c = rd(ST), rd(CFG)
            if s and c:
                self.show(s, c)
                self.events(s, c)
                self.s = s
        elif self.s and self.s["stage"] == "drain" and self.s["plugged"] and not self.s["cut"] \
                and time.time() - self.nag_t > 900:
            self.nag_t = time.time()
            notify("Still waiting, please unplug the charger to continue the calibration.")

    def show(self, s, c):
        if self.names != list(c["profiles"]):
            self.build_profiles(c)
        for n in self.names:
            self.prof[n].state = int(n == c["active"])
        self.nl.state = int(bool(c.get("notify_limit")))
        live = s["plugged"] or s["cut"]
        charging = live and (s["allowed"] or (macos(s) and s["pct"] < s["limit"]))
        state = "alert" if not s["smc_ok"] else "bolt" if charging else "plug" if live else "battery"
        self.icon = icons.icon(state, s["pct"])
        self.title = f" {s['pct']}%" + (" cal" if s["calibrating"] else "")
        self.strat.state = int(macos(s))
        t = once(s)
        self.once_x.title = f"Cancel charge to {t}%, back to profile" if t else "Cancel, back to profile"
        if (t is not None) != self.once_x_on:  # grey the item out while no one-time charge is running
            self.once_x.set_callback(self.charge_cancel if t else None)
            self.once_x_on = t is not None
        rng = f"macOS holds at {s['limit']}%" if macos(s) else f"{s['sail']}–{s['limit']}%"
        self.head.title = f"{s['pct']}%   " + {"paused": "limit paused", "ready": "charging for ready-by"}.get(
            s["profile"], f"{s['profile']} ({rng})")
        if not s["smc_ok"]:
            self.sub.title = "Charge control unavailable, run the probe command"
        elif s["msg"]:
            self.sub.title = s["msg"]
        elif s["cut"]:
            self.sub.title = f"Running on battery down to {s['sail']}%"
        elif not s["plugged"]:
            self.sub.title = "On battery"
        elif macos(s) and not s["allowed"]:
            self.sub.title = f"Holding at {s['limit']}%" if s["pct"] >= s["limit"] else f"Charging up to {s['limit']}%"
        else:
            self.sub.title = "Charging" if s["allowed"] else f"Holding, sailing down to {s['sail']}%"

    def events(self, s, c):
        p = self.s
        if p is None:  # first read after start, stay quiet
            return
        auto, st = s["mode"] == "adapter", s["stage"]
        if st != p["stage"]:
            if st == "charge":
                t = "Calibration started, charging to 100%."
            elif st == "hold":
                t = "Full. Holding at 100% for an hour."
            elif st == "drain":
                self.nag_t = time.time()
                t = "Draining now, the Mac runs on battery." if auto else "Please unplug the charger and keep using the Mac."
            elif st == "recharge":
                t = "10% reached, recharging." if auto else "10% reached, please plug the charger back in."
            elif st == "done":
                r = rd(RES) or {}
                m = r.get("measured_mah")
                h = f" ({r['health']:.1f}% of design)" if r.get("health") else ""
                t = f"Done. Measured capacity {m:.0f} mAh{h}." if m \
                    else "Done, but the drain was too short for a reliable measurement."
            else:
                t = "Calibration cancelled." if p["stage"] in ACTIVE else None
            if t:
                notify(t)
        elif c.get("notify_limit") and not s["calibrating"]:
            msgs = limit_events(p, s)
            if msgs:
                notify(" ".join(msgs))
        if s.get("warn") and s["warn"] != p.get("warn"):
            notify(s["warn"])
        if p["smc_ok"] and not s["smc_ok"]:
            notify("Charge control failed, the limit is not active.")

    def edit(self, _):
        front()
        c = rd(CFG)
        a = c["active"] if c.get("active") in c["profiles"] else None
        p = c["profiles"][a] if a else c.get("manual") or {"limit": 80, "sail": 70}
        w = rumps.Window("Format is name limit sail, for example gym 75 65", "Edit or add profile",
                         f"{a or 'custom'} {p['limit']} {p['sail']}", "Save", "Cancel", dimensions=(260, 24))
        r = w.run()
        try:
            name, lim, sail = r.text.split()
            lim, sail = int(lim), int(sail)
            assert r.clicked and 20 <= lim <= 100 and 5 <= sail < lim
        except Exception:
            return
        keep = (c["profiles"].get(name) or p).get("strategy")  # an edit keeps the strategy of the profile
        update(lambda c: (c["profiles"].__setitem__(name, dict({"limit": lim, "sail": sail}, **({"strategy": keep} if keep else {}))),
                          c.update(active=name)))

    def custom(self, _):
        front()
        s = rd(ST) or {}
        w = rumps.Window("Format is limit resume, for example 75 65. No profile needed.", "Set custom limit",
                         f"{s.get('limit', 80)} {s.get('sail', 70)}", "Set", "Cancel", dimensions=(260, 24))
        r = w.run()
        try:
            t = r.text.split()
            lim = int(t[0])
            sail = int(t[1]) if len(t) > 1 else max(5, lim - 10)
            assert r.clicked and 20 <= lim <= 100 and 5 <= sail < lim
        except Exception:
            return
        update(lambda c: c.update(manual=dict({"limit": lim, "sail": sail},
                                              **{k: v for k, v in (c.get("manual") or {}).items() if k == "strategy"}), active=None))

    def cal_start(self, _):
        front()
        s = rd(ST) or {}
        how = "The Mac then switches to battery by itself" if s.get("mode") == "adapter" \
            else "You then need to unplug it"
        if rumps.alert("Start calibration?", f"The Mac charges to 100% and rests for an hour. {how} until 10%, "
                       "then it recharges. This takes several hours, keep the lid open.", "Start", "Cancel") == 1:
            update(lambda c: c.update(calibrate="start"))

    def cal_result(self, _):
        front()
        r = rd(RES)
        if not r:
            return rumps.alert("No calibration result yet")
        m = r["measured_mah"]
        h = f" ({r['health']:.1f}% of design)" if r.get("health") else ""
        rumps.alert("Last calibration", f"Measured capacity {m:.0f} mAh{h}\n"
                    f"Gauge before {r['gauge_before']} mAh, after {r['gauge_after']} mAh\n"
                    f"Cycles {r['cycles']}, run {time.strftime('%d.%m.%Y', time.localtime(r['time']))}"
                    if m else "Drain was too short for a reliable measurement. Run it again from 100% to 10%.")

    def stats(self, _):
        front()
        s, r = rd(ST), rd(RES)
        if not s:
            return
        h = f"{s['health']:.1f}% ({s['raw_max']} of {s['design']} mAh)" if s["health"] else "unknown"
        rumps.alert("Battery statistics",
                    f"Health {h}\nCycles {s['cycles']}\nTemperature {'not reported' if s['temp'] is None else format(s['temp'], '.1f') + ' °C'}\n"
                    f"Power {s['volt'] * s['amps'] / 1e6:+.1f} W\n"
                    + (f"Calibrated capacity {r['measured_mah']:.0f} mAh\n" if r and r["measured_mah"] else "Not calibrated yet\n")
                    + f"Charge control {s['mode']}, updated {time.strftime('%H:%M', time.localtime(s['t']))}")


if __name__ == "__main__":
    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # menu bar only, no Dock icon
    App().run()
