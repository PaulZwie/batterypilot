#!/usr/bin/env python3
"""BatteryPilot menu bar app. Reads status.json, writes config.json, posts notifications."""
import json, os, subprocess, time
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


def above_limit(s):
    """Plugged in, charging held off, and the battery is higher than the limit, so it will drain down to it."""
    return bool(s["plugged"] and not s["allowed"] and not s["calibrating"] and s["pct"] > s["limit"])


def limit_events(p, s):
    """Messages about charging between two status snapshots, p before and s now."""
    lim, out = s["limit"], []
    start = s.get("override") == "full" and p.get("override") != "full"
    end = p.get("override") == "full" and not s.get("override")
    if start:
        out.append("Charging to 100% now." if s["plugged"] else "Will charge to 100% as soon as the charger is plugged in.")
    if end:
        out.append("Charged to 100%." if s["pct"] >= 99 else "Charge to 100% cancelled.")
    if above_limit(s) and not above_limit(p):
        out.append(f"Battery is above your limit of {lim}%, it will drain down to {lim}% before holding." if s["mode"] == "native"
                   else f"Battery is above your limit of {lim}%, charging stays off until it falls to {s['sail']}%.")
    elif above_limit(p) and s["plugged"] and not s["allowed"] and s["pct"] <= lim and not s["calibrating"]:
        out.append(f"Down to the limit of {lim}%, holding here.")
    elif not end and ((p["allowed"] and not s["allowed"]) or (s["mode"] == "native" and p["pct"] < lim <= s["pct"])):
        out.append(f"Limit of {lim}% reached.")
    if not p["allowed"] and s["allowed"] and s["plugged"] and not start and not s.get("override"):
        out.append(f"Charging resumed, limit is {lim}%.")
    return out


class App(rumps.App):
    def __init__(self):
        super().__init__("BatteryPilot", title="", icon=icons.icon("battery", 50), template=True, quit_button=None)
        self.head, self.sub = rumps.MenuItem("BatteryPilot"), rumps.MenuItem("")
        self.prof, self.cal = rumps.MenuItem("Profile"), rumps.MenuItem("Calibration")
        self.nl = rumps.MenuItem("Notify when limit is reached",
                                 callback=lambda _: update(lambda c: c.update(notify_limit=not c.get("notify_limit"))))
        self.cal.update([rumps.MenuItem("Start full calibration", callback=self.cal_start),
                         rumps.MenuItem("Cancel calibration", callback=lambda _: update(lambda c: c.update(calibrate="cancel"))),
                         rumps.MenuItem("Last result", callback=self.cal_result)])
        self.menu = [self.head, self.sub, None, self.prof,
                     rumps.MenuItem("Edit or add profile…", callback=self.edit),
                     rumps.MenuItem("Set custom limit…", callback=self.custom),
                     rumps.MenuItem("Charge to 100% once", callback=lambda _: update(lambda c: c.update(override="full"))),
                     self.nl, None, self.cal, rumps.MenuItem("Battery statistics…", callback=self.stats),
                     rumps.MenuItem("Open dashboard…", callback=lambda _: subprocess.Popen(["open", dash.start()])),
                     None, rumps.MenuItem("Quit", callback=self.quit_app)]
        self.names, self.s, self.mtime, self.nag_t = None, None, 0, 0
        try:
            self.tick(None)
        except Exception:
            pass

    def quit_app(self, _):
        """Unload the launch agent first, so launchd does not start the app again. The daemon keeps enforcing the limit."""
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/com.batterypilot.menu"])
        rumps.quit_application()  # reached when started by hand instead of by launchd

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
        charging = live and (s["allowed"] or (s["mode"] == "native" and s["pct"] < s["limit"]))
        state = "alert" if not s["smc_ok"] else "bolt" if charging else "plug" if live else "battery"
        self.icon = icons.icon(state, s["pct"])
        self.title = f" {s['pct']}%" + (" cal" if s["calibrating"] else "")
        rng = f"hold at {s['limit']}%" if s["mode"] == "native" else f"{s['sail']}–{s['limit']}%"
        self.head.title = f"{s['pct']}%   {s['profile']} ({rng})"
        if not s["smc_ok"]:
            self.sub.title = "Charge control unavailable, run the probe command"
        elif s["msg"]:
            self.sub.title = s["msg"]
        elif s["cut"]:
            self.sub.title = f"Running on battery down to {s['sail']}%"
        elif not s["plugged"]:
            self.sub.title = "On battery"
        elif s["mode"] == "native" and not s["allowed"]:
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
        update(lambda c: (c["profiles"].__setitem__(name, {"limit": lim, "sail": sail}), c.update(active=name)))

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
        update(lambda c: c.update(manual={"limit": lim, "sail": sail}, active=None))

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
