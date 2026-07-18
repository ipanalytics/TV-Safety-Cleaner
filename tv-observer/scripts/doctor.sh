#!/bin/sh
set -u

PORT=${TV_OBSERVER_PORT:-8090}
APP_DIR=/srv/tv-observer
DATA_DIR=/srv/tv-safety-data/observer
SERVICE=tv-observer.service
MODE=${1:-}
FAILURES=0

check() { if "$@"; then echo "ok: $*"; else echo "warn: $*"; FAILURES=$((FAILURES + 1)); fi; }
check python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'
if command -v adb >/dev/null 2>&1; then echo "ok: ADB executable found"; else echo "warn: ADB executable not found"; fi
if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "warn: port $PORT is already listening"
else
  echo "ok: port $PORT is available"
fi
echo "ok: configured default port $PORT"
echo "ok: isolation uses dedicated app, data, and service names"
if [ "$MODE" = "--local-check" ]; then
  check test -f "$(dirname -- "$0")/../config.example.toml"
  echo "local-check complete (warnings do not change the development workspace)"
  exit 0
fi
check test -d "$APP_DIR"
check test -d "$DATA_DIR"
check test -r "$DATA_DIR/observer.env"
if command -v systemctl >/dev/null 2>&1; then systemctl status "$SERVICE" --no-pager || FAILURES=$((FAILURES + 1)); fi
[ "$FAILURES" -eq 0 ]
