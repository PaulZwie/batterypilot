# BatteryPilot

A small menu bar app for Mac that protects your battery by stopping charging at a limit you choose, for example 80%. A battery that rests at 80% instead of sitting at 100% all day wears out more slowly.

- A slider for a custom limit, no profile needed, plus optional profiles such as home, work and travel
- A live dashboard with power flow, history and battery health
- One click to charge to 100% before a trip
- A calibration that measures your battery's real capacity
- Notifications when the limit is reached

> **Experimental.** BatteryPilot changes charging settings with administrator rights. It was written for personal use and is shared as is, with no warranty. If something looks wrong, uninstall it (see below), which hands charging back to macOS.

## Install

You need a Mac, an internet connection and your Mac password. The first run can take a few minutes.

1. Open **Terminal** (press `Cmd + Space`, type `Terminal`, press Enter).
2. Paste this line and press Enter:

   ```
   curl -fsSL https://raw.githubusercontent.com/PaulZwie/batterypilot/main/get.sh | bash
   ```
3. Type your Mac password when asked. Nothing appears while you type, that is normal. Press Enter.
4. If a window offers to install the "Command Line Tools", click Install and wait for it to finish. Then paste the line from step 2 again.
5. When it says **Done**, look for the battery icon in your menu bar.

**Prefer a download?** Click the green **Code** button on this page, choose **Download ZIP**, unzip it and double-click `Install.command`. If macOS refuses to open it, right-click the file and choose Open. If it still refuses, go to System Settings > Privacy & Security, scroll down and click **Open Anyway**.

You can read exactly what the installer does in [`install.sh`](install.sh) before running it.

## Use

In the dashboard (menu > Open dashboard) drag the limit slider to any value from 20 to 100 and it applies right away. Profiles are optional, and the Add button saves the current slider values as one. Click the battery icon in the menu bar to switch profile, edit a profile, set a custom limit, charge to 100% once, open the dashboard or start a calibration. Choose Quit to close the icon. Your limit stays active in the background even when the icon is closed. To bring the icon back, open **BatteryPilot** from your Applications folder (or press `Cmd + Space` and type BatteryPilot). It also starts by itself every time you log in.

For Terminal fans there is also a `bp` command:

```
bp                    show the current state
bp profile            list profiles, bp profile work switches
bp limit 75           custom limit, no profile needed
bp set gym 75 65      add or edit a profile (limit 75, resume at 65)
bp full               charge to 100% once
bp calibrate start    start a calibration (takes several hours)
bp stats              live battery statistics
bp probe              test that charge control works
```

## Check that it works

After installing, run `bp probe` in Terminal. It tells you which charge control method your Mac uses and whether it works. Then plug in the charger and watch whether the charge stops at your limit.

## Update

Run the same install line again. Your profiles and history are kept.

## Uninstall

Paste this in Terminal:

```
curl -fsSL https://raw.githubusercontent.com/PaulZwie/batterypilot/main/get.sh | bash -s -- --uninstall
```

Or double-click `Uninstall.command` from the downloaded ZIP. This removes everything, including your profiles and history, and charging goes back to normal.

## Requirements and limits

- macOS 26.7 or newer uses the built in macOS charge limit and needs nothing else.
- Older macOS versions need an `smc` command line tool, which is not included. <!-- TODO: say here where friends can get it --> Without it, limiting does not work there.
- Calibration takes several hours and needs the lid open.

## License

MIT, see [LICENSE](LICENSE).
