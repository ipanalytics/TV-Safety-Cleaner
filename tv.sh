#!/usr/bin/env bash
set -Eeuo pipefail

readonly BUNDLE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly OBSERVER_SOURCE="$BUNDLE_DIR/tv-observer"
readonly CONTROLLER_SOURCE="$BUNDLE_DIR/tv-controller"
readonly SHARED_SOURCE="$BUNDLE_DIR/tv-shared"
readonly SHARED_DESTINATION="/srv/tv-shared"
readonly DATA_ROOT="/srv/tv-safety-data"
readonly OBSERVER_ENV="$DATA_ROOT/observer/observer.env"

log() {
  printf '[tv-safety] %s\n' "$*"
}

fail() {
  printf '[tv-safety] ERROR: %s\n' "$*" >&2
  exit 1
}

validate_bundle() {
  local required
  for required in \
    "$BUNDLE_DIR/README.md" \
    "$SHARED_SOURCE/pyproject.toml" \
    "$SHARED_SOURCE/src/tv_safety_shared/ui.py" \
    "$SHARED_SOURCE/src/tv_safety_shared/templates/suite_base.html" \
    "$SHARED_SOURCE/src/tv_safety_shared/static/suite.css" \
    "$OBSERVER_SOURCE/requirements.txt" \
    "$OBSERVER_SOURCE/scripts/install-raspberry.sh" \
    "$OBSERVER_SOURCE/scripts/rebuild-raspberry.sh" \
    "$OBSERVER_SOURCE/systemd/tv-observer.service" \
    "$CONTROLLER_SOURCE/requirements.txt" \
    "$CONTROLLER_SOURCE/scripts/install-raspberry.sh" \
    "$CONTROLLER_SOURCE/scripts/rebuild-raspberry.sh" \
    "$CONTROLLER_SOURCE/systemd/tv-controller.service"; do
    [[ -f "$required" ]] || fail "Incomplete upload: missing ${required#"$BUNDLE_DIR/"}"
  done

  bash -n "$BUNDLE_DIR/tv.sh"
  bash -n "$OBSERVER_SOURCE"/scripts/*.sh
  bash -n "$CONTROLLER_SOURCE"/scripts/*.sh
  log "Bundle structure and shell syntax are valid."
  log "Observer source: $OBSERVER_SOURCE"
  log "Controller source: $CONTROLLER_SOURCE"
  log "Shared UI source: $SHARED_SOURCE"
}

RESET_PASSWORD=0
case "${1:-}" in
  --check)
    [[ $# -eq 1 ]] || fail "Usage: $0 [--check|--reset-password]"
    validate_bundle
    exit 0
    ;;
  --reset-password)
    [[ $# -eq 1 ]] || fail "Usage: $0 [--check|--reset-password]"
    RESET_PASSWORD=1
    ;;
  "") ;;
  *) fail "Usage: $0 [--check|--reset-password]" ;;
esac

validate_bundle

if [[ $EUID -ne 0 ]]; then
  command -v sudo >/dev/null 2>&1 || fail "Run as root or install sudo."
  log "Requesting root privileges through sudo."
  exec sudo --preserve-env=TV_OBSERVER_ADMIN_PASSWORD bash "$0" "$@"
fi

command -v apt-get >/dev/null 2>&1 || fail "This installer requires DietPi/Debian with apt-get."
command -v systemctl >/dev/null 2>&1 || fail "systemd is required."

mkdir -p /run/lock
exec 9>/run/lock/tv-safety-deploy.lock
command -v flock >/dev/null 2>&1 || {
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y util-linux
}
flock -n 9 || fail "Another TV Safety deployment is already running."

install_prerequisites() {
  local package
  local -a missing=()
  for package in ca-certificates python3 python3-venv adb rsync; do
    dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'install ok installed' || \
      missing+=("$package")
  done
  if ((${#missing[@]})); then
    log "Installing missing OS packages: ${missing[*]}"
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
  else
    log "OS prerequisites are already installed."
  fi
  python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' || \
    fail "Python 3.11 or newer is required by both projects."
}

ensure_service_identity() {
  getent group tv-safety >/dev/null || groupadd --system tv-safety
  if ! getent passwd tv-safety >/dev/null; then
    useradd --system --gid tv-safety --home-dir "$DATA_ROOT" \
      --shell /usr/sbin/nologin tv-safety
  fi
}

prepare_source_permissions() {
  chmod 0755 "$OBSERVER_SOURCE"/scripts/*.sh "$CONTROLLER_SOURCE"/scripts/*.sh
  chmod 0644 "$OBSERVER_SOURCE"/systemd/*.service "$CONTROLLER_SOURCE"/systemd/*.service
}

cleanup_uploaded_bundle() {
  local cache_dir retired_file
  local -a retired_files=(
    "$OBSERVER_SOURCE/src/tv_observer/adguard.py"
    "$OBSERVER_SOURCE/src/tv_observer/dns.py"
    "$OBSERVER_SOURCE/tests/test_adguard.py"
    "$OBSERVER_SOURCE/tests/test_dns.py"
    "$OBSERVER_SOURCE/docs/DNS_OBSERVATION.md"
  )
  for retired_file in "${retired_files[@]}"; do
    find "$(dirname -- "$retired_file")" -maxdepth 1 \
      -name "$(basename -- "$retired_file")" -delete
  done
  find "$OBSERVER_SOURCE" "$CONTROLLER_SOURCE" "$SHARED_SOURCE" \
    -type f \( -name '*.pyc' -o -name '.DS_Store' -o -name '._*' \) -delete
  while IFS= read -r -d '' cache_dir; do
    find "$cache_dir" -depth -delete
  done < <(
    find "$OBSERVER_SOURCE" "$CONTROLLER_SOURCE" "$SHARED_SOURCE" -type d \
      \( -name '__pycache__' -o -name '.pytest_cache' -o -name '.ruff_cache' \
      -o -name '.mypy_cache' -o -name '*.egg-info' \) -prune -print0
  )
  log "Uploaded bundle cleaned of retired modules and generated tool artifacts."
}

deploy_shared_ui() {
  install -d -o root -g tv-safety -m 0750 "$SHARED_DESTINATION"
  rsync -a --delete \
    --exclude '.git/' --exclude '.pytest_cache/' --exclude '.ruff_cache/' \
    --exclude '.mypy_cache/' --exclude '__pycache__/' --exclude '*.egg-info/' \
    --exclude '.DS_Store' --exclude '._*' \
    "$SHARED_SOURCE/" "$SHARED_DESTINATION/"
  chown -R root:tv-safety "$SHARED_DESTINATION"
  log "Shared TV Safety UI deployed once for both services."
}

deploy_part() {
  local name="$1"
  local source="$2"
  local destination="$3"
  local action
  if [[ -x "$destination/.venv/bin/python" ]]; then
    action="rebuild"
    log "$name already exists; running an in-place update."
    bash "$source/scripts/rebuild-raspberry.sh"
  else
    action="install"
    log "$name is not installed; running first installation."
    bash "$source/scripts/install-raspberry.sh"
  fi
  log "$name $action completed."
}

cleanup_retired_features() {
  local observer_root="$DATA_ROOT/observer"
  local retired_imports="$observer_root/imports"

  find "$observer_root" -maxdepth 1 -name 'adguard-settings.json' -delete
  if [[ -L "$retired_imports" ]]; then
    find "$observer_root" -maxdepth 1 -type l -name 'imports' -delete
  elif [[ -d "$retired_imports" ]]; then
    find "$retired_imports" -xdev -mindepth 1 -delete
    rmdir "$retired_imports"
  fi
  log "Retired AdGuard settings and DNS import storage cleaned. Snapshots and backups preserved."
}

configure_observer_credentials() {
  local secret password password_again password_hash generated=0 network_changed=0
  install -d -o tv-safety -g tv-safety -m 0750 "$DATA_ROOT/observer"
  touch "$OBSERVER_ENV"
  chown tv-safety:tv-safety "$OBSERVER_ENV"
  chmod 0600 "$OBSERVER_ENV"

  if ! grep -q '^TV_OBSERVER_SECRET_KEY=' "$OBSERVER_ENV"; then
    secret="$(/srv/tv-observer/.venv/bin/python -c 'import secrets; print(secrets.token_hex(32))')"
    printf 'TV_OBSERVER_SECRET_KEY=%s\n' "$secret" >>"$OBSERVER_ENV"
    generated=1
  fi

  if ! grep -q '^TV_OBSERVER_PASSWORD_HASH=' "$OBSERVER_ENV" || \
      ((RESET_PASSWORD)) || [[ -n "${TV_OBSERVER_ADMIN_PASSWORD:-}" ]]; then
    password="${TV_OBSERVER_ADMIN_PASSWORD:-}"
    if [[ -z "$password" && -t 0 ]]; then
      read -r -s -p 'New TV Observer web password (leave empty to generate): ' password
      printf '\n'
      if [[ -n "$password" ]]; then
        read -r -s -p 'Repeat password: ' password_again
        printf '\n'
        [[ "$password" == "$password_again" ]] || fail "Passwords do not match."
      fi
    fi
    if [[ -z "$password" && ! -t 0 ]]; then
      fail "First non-interactive install requires TV_OBSERVER_ADMIN_PASSWORD."
    fi
    if [[ -z "$password" ]]; then
      password="$(/srv/tv-observer/.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(18))')"
      printf '\nGenerated one-time TV Observer password: %s\n' "$password" >/dev/tty
    fi
    ((${#password} >= 6)) || fail "TV Observer password must contain at least 6 characters."
    password_hash="$(printf '%s' "$password" | /srv/tv-observer/.venv/bin/python -c \
      'import sys; from werkzeug.security import generate_password_hash; print(generate_password_hash(sys.stdin.read()))')"
    if grep -q '^TV_OBSERVER_PASSWORD_HASH=' "$OBSERVER_ENV"; then
      sed -i "s|^TV_OBSERVER_PASSWORD_HASH=.*|TV_OBSERVER_PASSWORD_HASH=$password_hash|" \
        "$OBSERVER_ENV"
    else
      printf 'TV_OBSERVER_PASSWORD_HASH=%s\n' "$password_hash" >>"$OBSERVER_ENV"
    fi
    unset password password_again password_hash
    generated=1
  fi

  if grep -q '^TV_OBSERVER_ALLOW_LAN=' "$OBSERVER_ENV"; then
    if ! grep -q '^TV_OBSERVER_ALLOW_LAN=true$' "$OBSERVER_ENV"; then
      sed -i 's/^TV_OBSERVER_ALLOW_LAN=.*/TV_OBSERVER_ALLOW_LAN=true/' "$OBSERVER_ENV"
      network_changed=1
    fi
  else
    printf 'TV_OBSERVER_ALLOW_LAN=true\n' >>"$OBSERVER_ENV"
    network_changed=1
  fi

  if grep -q '^TV_OBSERVER_TRUSTED_CIDRS=' "$OBSERVER_ENV"; then
    if ! grep -q '^TV_OBSERVER_TRUSTED_CIDRS=127\.0\.0\.0/8,::1/128,10\.0\.0\.0/8,172\.16\.0\.0/12,192\.168\.0\.0/16$' "$OBSERVER_ENV"; then
      sed -i 's|^TV_OBSERVER_TRUSTED_CIDRS=.*|TV_OBSERVER_TRUSTED_CIDRS=127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16|' "$OBSERVER_ENV"
      network_changed=1
    fi
  else
    printf 'TV_OBSERVER_TRUSTED_CIDRS=127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16\n' >>"$OBSERVER_ENV"
    network_changed=1
  fi
  chown tv-safety:tv-safety "$OBSERVER_ENV"
  chmod 0600 "$OBSERVER_ENV"

  systemctl enable tv-observer.service
  if ((generated || network_changed)); then
    systemctl restart tv-observer.service
  else
    systemctl start tv-observer.service
  fi
}

configure_local_firewall() {
  local cidr port
  if command -v ufw >/dev/null 2>&1 && ufw status | grep -q '^Status: active'; then
    for port in 8090 8091; do
      for cidr in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16; do
        ufw allow from "$cidr" to any port "$port" proto tcp >/dev/null
      done
    done
    log "UFW allows TV Observer and Controller on TCP 8090-8091 from private networks."
  fi
}

record_deployment() {
  install -d -o tv-safety -g tv-safety -m 0750 "$DATA_ROOT/logs"
  printf '%s observer=%s controller=%s source=%s\n' \
    "$(date --iso-8601=seconds)" \
    "$(/srv/tv-observer/.venv/bin/python -c 'import tv_observer; print(tv_observer.__version__)')" \
    "$(/srv/tv-controller/.venv/bin/python -c 'import tv_controller; print(tv_controller.__version__)')" \
    "$BUNDLE_DIR" >>"$DATA_ROOT/logs/deployments.log"
  chown tv-safety:tv-safety "$DATA_ROOT/logs/deployments.log"
  chmod 0640 "$DATA_ROOT/logs/deployments.log"
}

install_prerequisites
ensure_service_identity
cleanup_uploaded_bundle
prepare_source_permissions
deploy_shared_ui
deploy_part "TV Observer" "$OBSERVER_SOURCE" /srv/tv-observer
configure_observer_credentials
cleanup_retired_features
configure_local_firewall
deploy_part "TV Controller" "$CONTROLLER_SOURCE" /srv/tv-controller
record_deployment

systemctl daemon-reload
systemctl is-failed --quiet tv-observer.service && fail "tv-observer.service is in a failed state."
systemctl is-active --quiet tv-observer.service || fail "tv-observer.service is not running."
systemctl is-failed --quiet tv-controller.service && fail "tv-controller.service is in a failed state."
systemctl is-active --quiet tv-controller.service || fail "tv-controller.service is not running."

log "Deployment complete. Existing private data and backups were not removed."
log "Observer: http://<raspberry-pi-ip>:8090 (for example http://192.168.0.111:8090)"
log "Controller: http://<raspberry-pi-ip>:8091 (for example http://192.168.0.111:8091)"
log "Upload this whole bundle again and rerun this same file for future updates."
