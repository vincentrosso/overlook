#!/usr/bin/env bash
# Run Overlook at login on macOS (LaunchAgent). Undo: scripts/install-launchd.sh --uninstall
set -euo pipefail
LABEL=com.overlook.dashboard
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true; rm -f "$PLIST"; echo "removed $LABEL"; exit 0
fi
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$(command -v python3)</string><string>$DIR/overlook.py</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$PATH</string>
    ${CLAUDE_CONFIG_DIR:+<key>CLAUDE_CONFIG_DIR</key><string>$CLAUDE_CONFIG_DIR</string>}
  </dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/overlook.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/overlook.log</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Overlook running at http://overlook.localhost:7878 (logs: ~/Library/Logs/overlook.log)"
echo "Want it without the port? sudo scripts/port80.sh  ->  http://overlook.localhost"
