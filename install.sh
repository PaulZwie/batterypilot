#!/bin/bash
# BatteryPilot installer.
#   ./install.sh [/path/to/smc]   install or update (profiles and history are kept)
#   ./install.sh --uninstall      remove everything and give charging back to macOS
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/src"
APP="/Applications/BatteryPilot.app"
D="/Library/Application Support/BatteryPilot"
DP=/Library/LaunchDaemons/com.batterypilot.daemon.plist
AP="$HOME/Library/LaunchAgents/com.batterypilot.menu.plist"
PYTHON_VERSION="3.11.9"
PYTHON_PKG_URL="https://www.python.org/ftp/python/${PYTHON_VERSION}/python-${PYTHON_VERSION}-macos11.pkg"

[ "$(uname)" = Darwin ] || { echo "BatteryPilot only runs on a Mac."; exit 1; }
[ "$(id -u)" != 0 ] || { echo "Run this without sudo. It asks for your password when needed."; exit 1; }

if [ "$1" = "--uninstall" ]; then
  echo "Removing BatteryPilot, this asks for your password."
  sudo -v
  launchctl bootout "gui/$(id -u)/com.batterypilot.menu" 2>/dev/null || true
  sudo launchctl bootout system/com.batterypilot.daemon 2>/dev/null || true
  sudo rm -rf "$DP" "$AP" "$D" /usr/local/libexec/batterypilotd.py /usr/local/bin/bp "$APP" "$HOME/.batterypilot"
  echo "Removed. Charging is back to normal."; exit 0
fi

PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  echo "Python 3 is not installed. Downloading the official Python ${PYTHON_VERSION} package..."
  PKG="$(mktemp -t batterypilot-python).pkg"
  trap 'rm -f "$PKG"' EXIT
  if ! curl -fsSL --retry 5 --retry-delay 3 "$PYTHON_PKG_URL" -o "$PKG"; then
    echo "Could not download Python from python.org. Check your internet connection and try again."
    exit 1
  fi
  echo "Installing Python (macOS will ask for your password)..."
  sudo installer -pkg "$PKG" -target /
  rm -f "$PKG"
  trap - EXIT
  PY="$(command -v python3 || true)"
  [ -n "$PY" ] || PY="/usr/local/bin/python3"
  [ -x "$PY" ] || {
    echo "Python was installed, but its executable could not be found. Please restart Terminal and run the install link again."
    exit 1
  }
fi

SMC_BIN="${1:-$(command -v smc || true)}"
[ -x "$SMC_BIN" ] || echo "No smc tool found. That is fine on macOS 26.7 or newer, which uses the macOS charge limit instead."

echo "Installing BatteryPilot, this asks for your password once."
sudo -v
sudo launchctl bootout system/com.batterypilot.daemon 2>/dev/null || true  # stop the old daemon first, it hands back the macOS limit
sudo mkdir -p "$D/cfg" /usr/local/libexec /usr/local/bin
sudo chown root:wheel "$D" && sudo chmod 755 "$D"
sudo chown root:admin "$D/cfg" && sudo chmod 775 "$D/cfg"
sudo install -m 755 "$SRC/daemon.py" /usr/local/libexec/batterypilotd.py
sudo install -m 755 "$SRC/bp" /usr/local/bin/bp
sudo tee "$DP" >/dev/null <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.batterypilot.daemon</string>
<key>ProgramArguments</key><array><string>$PY</string><string>/usr/local/libexec/batterypilotd.py</string></array>
<key>EnvironmentVariables</key><dict><key>SMC</key><string>$SMC_BIN</string></dict>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
</dict></plist>
PLIST
sudo chown root:wheel "$DP"
echo "Probing charge control (on newer macOS this takes up to 70 seconds)..."
sudo SMC="$SMC_BIN" "$PY" /usr/local/libexec/batterypilotd.py probe
sudo launchctl bootstrap system "$DP"

echo "Installing the menu bar app (needs internet for one small download)..."
mkdir -p "$HOME/.batterypilot" "$HOME/Library/LaunchAgents"
[ -d "$HOME/.batterypilot/venv" ] || "$PY" -m venv "$HOME/.batterypilot/venv"
VENV_PY="$HOME/.batterypilot/venv/bin/python"
# rumps does not cap its PyObjC dependency. PyObjC 13 requires Python 3.11,
# while Apple's Command Line Tools may provide Python 3.9.
"$VENV_PY" -m pip install -q --upgrade pip setuptools wheel
"$VENV_PY" -m pip install -q --upgrade --only-binary=pyobjc-core,pyobjc-framework-Cocoa \
  "rumps==0.4.0" "pyobjc-framework-Cocoa<13"
cp "$SRC/menubar.py" "$SRC/dash.py" "$SRC/icons.py" "$SRC/ui.html" "$HOME/.batterypilot/"
cat > "$AP" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.batterypilot.menu</string>
<key>ProgramArguments</key><array><string>$HOME/.batterypilot/venv/bin/python</string><string>$HOME/.batterypilot/menubar.py</string></array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
</dict></plist>
PLIST
launchctl bootout "gui/$(id -u)/com.batterypilot.menu" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$AP"
echo "Adding BatteryPilot to your Applications folder..."
sudo rm -rf "$APP"
sudo mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
sudo install -m 755 "$HERE/app/launcher.sh" "$APP/Contents/MacOS/BatteryPilot"
sudo cp "$HERE/app/Info.plist" "$APP/Contents/Info.plist"
sudo cp "$HERE/app/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
sudo codesign --force --deep -s - "$APP" >/dev/null 2>&1 || true
sudo touch "$APP"
echo
[ -f "$HERE/VERSION" ] && sudo cp "$HERE/VERSION" "$D/version"  # lets bp update know what is installed
echo "Done. Look for the battery in your menu bar."
echo "If you quit it, open BatteryPilot from your Applications folder (or Spotlight) to bring it back."
echo "Terminal helper: bp   (try: bp status, bp profile, bp stats)"
