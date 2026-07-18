#!/bin/sh
set -eu

[ "$(id -u)" -eq 0 ] || { echo "Run as root" >&2; exit 1; }
systemctl disable --now tv-controller.service 2>/dev/null || true
rm -f /etc/systemd/system/tv-controller.service
systemctl daemon-reload
rm -rf /srv/tv-controller
if [ "${1:-}" = "--delete-project-data" ] && [ "${2:-}" = "I_UNDERSTAND" ]; then
  rm -rf /srv/tv-safety-data/controller
fi
echo "Backups in /srv/tv-safety-data/backups were preserved."
