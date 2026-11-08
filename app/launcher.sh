#!/bin/bash
# Starts the BatteryPilot menu bar app again after it was quit. The charge limit itself runs in the background all the time.
L=com.batterypilot.menu
U="$(id -u)"
PLIST="$HOME/Library/LaunchAgents/$L.plist"
if launchctl print "gui/$U/$L" 2>/dev/null | grep -q "state = running"; then
  osascript -e 'display notification "BatteryPilot is already running in the menu bar." with title "BatteryPilot"'
elif [ -f "$PLIST" ]; then
  launchctl print "gui/$U/$L" >/dev/null 2>&1 || launchctl bootstrap "gui/$U" "$PLIST"
  launchctl kickstart "gui/$U/$L" 2>/dev/null || true
else
  osascript -e 'display alert "BatteryPilot is not installed" message "Run the BatteryPilot installer again."'
fi
