#!/bin/bash
# One line installer. Downloads BatteryPilot from GitHub into a temporary folder and runs install.sh.
set -e
REPO="${BP_REPO:-PaulZwie/batterypilot}"   # change PaulZwie to your GitHub user name
BRANCH="${BP_BRANCH:-main}"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
echo "Downloading BatteryPilot..."
curl -fsSL "https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz" | tar -xz -C "$T" --strip-components=1
bash "$T/install.sh" "$@"
