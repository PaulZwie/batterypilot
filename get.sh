#!/bin/bash
# One line installer. Downloads BatteryPilot from GitHub into a temporary folder and runs install.sh.
set -e
REPO="${BP_REPO:-PaulZwie/batterypilot}"
REF="${BP_REF:-${BP_BRANCH:-main}}"  # a branch or a release tag like v1.2
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
echo "Downloading BatteryPilot..."
if ! curl -fsSL --retry 5 --retry-delay 5 "https://github.com/$REPO/archive/$REF.tar.gz" -o "$T/bp.tar.gz"; then
  echo "Could not download BatteryPilot from GitHub. GitHub may be having a short outage, please try again in a few minutes."
  exit 1
fi
tar -xz -f "$T/bp.tar.gz" -C "$T" --strip-components=1
[ -f "$T/install.sh" ] || { echo "The download looks incomplete. Please try again."; exit 1; }
bash "$T/install.sh" "$@"
