#!/bin/sh
set -eu

CONFIG=/srv/tv-safety-data/controller/controller.env
SERVICE=tv-controller.service
[ "$(id -u)" -eq 0 ] || { echo "Run as root" >&2; exit 1; }
if [ -f "$CONFIG" ]; then BEFORE=$(sha256sum "$CONFIG" | cut -d' ' -f1); else BEFORE=missing; fi
"$(dirname -- "$0")/install-raspberry.sh"
if [ "$BEFORE" != missing ]; then
  AFTER=$(sha256sum "$CONFIG" | cut -d' ' -f1)
  [ "$BEFORE" = "$AFTER" ] || { echo "Private config changed unexpectedly" >&2; exit 1; }
fi
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"
