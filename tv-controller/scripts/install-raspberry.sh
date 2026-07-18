#!/bin/sh
set -eu

APP_DIR=/srv/tv-controller
DATA_ROOT=/srv/tv-safety-data
SERVICE=tv-controller.service
SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

[ "$(id -u)" -eq 0 ] || { echo "Run as root" >&2; exit 1; }
python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'
command -v rsync >/dev/null 2>&1 || { echo "rsync is required; use tv.sh" >&2; exit 1; }
if ! getent group tv-safety >/dev/null 2>&1; then groupadd --system tv-safety; fi
if ! getent passwd tv-safety >/dev/null 2>&1; then
  useradd --system --gid tv-safety --home-dir "$DATA_ROOT" --shell /usr/sbin/nologin tv-safety
fi
install -d -o tv-safety -g tv-safety -m 0750 "$APP_DIR"
install -d -o tv-safety -g tv-safety -m 0750 \
  "$DATA_ROOT/observer" "$DATA_ROOT/controller" "$DATA_ROOT/backups" "$DATA_ROOT/logs"
install -d -o tv-safety -g tv-safety -m 0750 \
  "$DATA_ROOT/controller/apks" "$DATA_ROOT/controller/apks/_tasks" \
  "$DATA_ROOT/controller/profiles"
install -d -o tv-safety -g tv-safety -m 0700 "$DATA_ROOT/controller/.android"
if [ -d "$DATA_ROOT/.android" ] && [ ! -L "$DATA_ROOT/.android" ]; then
  rsync -a --ignore-existing "$DATA_ROOT/.android/" "$DATA_ROOT/controller/.android/"
  find "$DATA_ROOT/.android" -xdev -mindepth 1 -delete
  rmdir "$DATA_ROOT/.android"
fi
chown -R tv-safety:tv-safety "$DATA_ROOT/controller/.android"
chmod 0700 "$DATA_ROOT/controller/.android"
systemctl stop "$SERVICE" 2>/dev/null || true
find "$DATA_ROOT/controller/apks/_tasks" -xdev -type f -name '*.json' -delete
case "$APP_DIR" in /srv/tv-controller) ;; *) echo "Unsafe app destination" >&2; exit 1 ;; esac
rsync -a --delete \
  --exclude '.venv/' --exclude '.git/' --exclude '.pytest_cache/' --exclude '.ruff_cache/' \
  --exclude '.mypy_cache/' --exclude '__pycache__/' --exclude '*.egg-info/' \
  --exclude '.DS_Store' --exclude '._*' --exclude 'src/instance/' \
  --exclude 'config.toml' --exclude '*.local.toml' --exclude '.env' \
  "$SOURCE_DIR/" "$APP_DIR/"
chown -R root:tv-safety "$APP_DIR"
chmod 0750 "$APP_DIR"
chmod 0755 "$APP_DIR"/scripts/*.sh
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
"$APP_DIR/.venv/bin/pip" install --no-deps --editable /srv/tv-shared
"$APP_DIR/.venv/bin/pip" install --no-deps --editable "$APP_DIR"
install -m 0644 "$SOURCE_DIR/systemd/$SERVICE" "/etc/systemd/system/$SERVICE"
systemctl daemon-reload
if grep -q '^TV_OBSERVER_SECRET_KEY=' "$DATA_ROOT/observer/observer.env" && \
   grep -q '^TV_OBSERVER_PASSWORD_HASH=' "$DATA_ROOT/observer/observer.env"; then
  systemctl enable --now "$SERVICE"
else
  echo "$SERVICE installed but not started: shared credentials are incomplete." >&2
fi
