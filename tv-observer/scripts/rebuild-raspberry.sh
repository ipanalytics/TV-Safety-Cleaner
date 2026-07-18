#!/bin/sh
set -eu

ENV_FILE=/srv/tv-safety-data/observer/observer.env
SERVICE=tv-observer.service
[ "$(id -u)" -eq 0 ] || { echo "Run as root" >&2; exit 1; }
if systemctl is-enabled --quiet "$SERVICE" 2>/dev/null; then WAS_ENABLED=yes; else WAS_ENABLED=no; fi
if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then WAS_ACTIVE=yes; else WAS_ACTIVE=no; fi
if [ -f "$ENV_FILE" ]; then
  BEFORE=$(sha256sum "$ENV_FILE" | cut -d' ' -f1)
else
  BEFORE=missing
fi
"$(dirname -- "$0")/install-raspberry.sh"
if [ "$BEFORE" != missing ]; then
  AFTER=$(sha256sum "$ENV_FILE" | cut -d' ' -f1)
  [ "$BEFORE" = "$AFTER" ] || { echo "Private environment changed unexpectedly" >&2; exit 1; }
fi
if [ "$WAS_ENABLED" = no ]; then
  systemctl disable "$SERVICE" 2>/dev/null || true
fi
if [ "$WAS_ACTIVE" = yes ]; then
  systemctl restart "$SERVICE"
else
  systemctl stop "$SERVICE" 2>/dev/null || true
fi
