#!/bin/bash
# OmarchyMac installer (run on the Mac). Builds a venv, installs the LaunchAgent
# and starts the menu-bar daemon. No sudo.
set -euo pipefail

APP_DIR="$HOME/omarchy-mac-link"
STATE_DIR="$HOME/.config/omarchy-mac-link"
AGENT="$HOME/Library/LaunchAgents/com.mrg.omarchymac.plist"
LABEL="com.mrg.omarchymac"

cd "$APP_DIR"
mkdir -p "$STATE_DIR"

echo "==> venv + rumps"
python3 -m venv venv
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet rumps pyobjc-framework-Quartz
./venv/bin/python -c "import rumps, otv_crypto; print('deps ok, rumps', rumps.__version__)"

echo "==> LaunchAgent"
cp -f com.mrg.omarchymac.plist "$AGENT"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$AGENT"
launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null || true

sleep 2
echo "==> status"
launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -E "state|pid" | head -3 || true
echo "OmarchyMac is running. Open the menu-bar icon and click 'Pair a new computer'."
