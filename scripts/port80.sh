#!/usr/bin/env bash
# Make http://overlook.localhost (no port) reach Overlook on 7878, via a pf redirect on loopback.
# Needs sudo once; persists across reboots (pf loads /etc/pf.conf at boot). Undo: sudo scripts/port80.sh --uninstall
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
PORT="${OVERLOOK_PORT:-7878}"
ANCHOR=/etc/pf.anchors/overlook
if [[ "${1:-}" == "--uninstall" ]]; then
  sed -i '' '/^# overlook$/,/^load anchor "overlook"/d' /etc/pf.conf
  rm -f "$ANCHOR"; pfctl -f /etc/pf.conf 2>/dev/null || true; echo "removed overlook pf anchor"; exit 0
fi
cat > "$ANCHOR" <<PF
rdr pass on lo0 inet proto tcp from any to 127.0.0.1 port 80 -> 127.0.0.1 port $PORT
PF
if ! grep -q '^rdr-anchor "overlook"' /etc/pf.conf; then
  printf '\n# overlook\nrdr-anchor "overlook"\nload anchor "overlook" from "%s"\n' "$ANCHOR" >> /etc/pf.conf
fi
pfctl -f /etc/pf.conf 2>/dev/null; pfctl -e 2>/dev/null || true
echo "http://overlook.localhost -> 127.0.0.1:$PORT (pf rdr, persistent)"
