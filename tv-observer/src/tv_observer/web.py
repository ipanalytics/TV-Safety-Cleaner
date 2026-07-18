from __future__ import annotations

import hmac
import ipaddress
import json
import os
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Callable
from pathlib import Path

from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from tv_safety_shared import install_shared_ui
from werkzeug.security import check_password_hash
from werkzeug.wrappers import Response as BaseResponse

from tv_observer.adb import AdbError, ReadOnlyAdb
from tv_observer.collector import collect_snapshot_payload
from tv_observer.recovery import default_readiness_report
from tv_observer.snapshot import SnapshotError, create_snapshot, list_snapshots

SECTIONS = (
    "Overview",
    "Connect",
    "Applications",
    "Snapshots",
    "Observe",
    "Recovery",
    "Reports",
    "Settings",
    "Diagnostics",
)

DeviceReader = Callable[[], list[dict[str, str]]]
SnapshotCapture = Callable[[Path, str, str], Path]


def _env_value(path: Path, key: str) -> str:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("credential file is unavailable")
    prefix = f"{key}="
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return line.removeprefix(prefix)
    raise RuntimeError(f"credential value is missing: {key}")


def _set_env_value(path: Path, key: str, value: str) -> None:
    if not path.is_file() or path.is_symlink() or "\n" in value:
        raise RuntimeError("credential file cannot be updated safely")
    prefix = f"{key}="
    lines = path.read_text(encoding="utf-8").splitlines()
    updated = False
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = f"{prefix}{value}"
            updated = True
            break
    if not updated:
        lines.append(f"{prefix}{value}")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _read_devices() -> list[dict[str, str]]:
    return [
        {"serial": device.serial, "state": device.state.value, "details": device.details}
        for device in ReadOnlyAdb().devices()
    ]


def _capture_snapshot(root: Path, device_name: str, serial: str) -> Path:
    return create_snapshot(root, device_name, collect_snapshot_payload(serial))


def _snapshot_state(root: Path) -> dict[str, object]:
    snapshots = list_snapshots(root)
    rows: list[dict[str, object]] = []
    latest: dict[str, object] | None = None
    latest_path: Path | None = None
    for path in reversed(snapshots):
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        row = {
            "path": str(path),
            "device": path.parent.name,
            "created": manifest.get("created_local", "unknown"),
            "model": manifest.get("model", "unknown"),
            "platform": manifest.get("platform", "unknown"),
            "firmware": manifest.get("firmware", "unknown"),
            "packages": manifest.get("package_count", 0),
        }
        rows.append(row)
        if latest is None:
            latest = {
                "manifest": manifest,
                "device": json.loads((path / "device.json").read_text(encoding="utf-8")),
                "firmware": json.loads((path / "firmware.json").read_text(encoding="utf-8")),
                "packages": json.loads((path / "packages.json").read_text(encoding="utf-8")),
                "disabled": json.loads(
                    (path / "disabled-packages.json").read_text(encoding="utf-8")
                ),
                "settings": json.loads((path / "settings.json").read_text(encoding="utf-8")),
                "processes": json.loads((path / "processes.json").read_text(encoding="utf-8")),
                "network": json.loads(
                    (path / "network-summary.json").read_text(encoding="utf-8")
                ),
                "path": str(path),
            }
            latest_path = path
    return {"snapshots": rows, "latest": latest, "latest_path": latest_path}


class RateLimiter:
    def __init__(self, limit: int = 5, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        events = self._events[key]
        while events and events[0] < now - self.window_seconds:
            events.popleft()
        if len(events) >= self.limit:
            return False
        events.append(now)
        return True


def _csrf_token() -> str:
    token = session.get("csrf_token")
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _valid_cidr(address: str, trusted: tuple[str, ...]) -> bool:
    try:
        client = ipaddress.ip_address(address)
        return any(client in ipaddress.ip_network(cidr, strict=False) for cidr in trusted)
    except ValueError:
        return False


def create_app(
    *,
    secret_key: str,
    password_hash: str,
    data_root: Path,
    snapshot_root: Path | None = None,
    device_reader: DeviceReader = _read_devices,
    snapshot_capture: SnapshotCapture = _capture_snapshot,
    credential_file: Path | None = None,
    allow_lan: bool = False,
    trusted_cidrs: tuple[str, ...] = ("127.0.0.0/8", "::1/128"),
    cookie_secure: bool = False,
) -> Flask:
    if allow_lan and not trusted_cidrs:
        raise ValueError("LAN access requires at least one trusted CIDR")
    app = Flask(__name__)
    install_shared_ui(app)
    app.config.update(
        SECRET_KEY=secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=cookie_secure,
    )
    app.jinja_env.globals["csrf_token"] = _csrf_token
    limiter = RateLimiter()
    data_root.mkdir(parents=True, exist_ok=True)
    inventory_root = snapshot_root or data_root / "snapshots"
    inventory_root.mkdir(parents=True, exist_ok=True)
    app.config["SNAPSHOT_ROOT"] = inventory_root

    def current_password_hash() -> str:
        return (
            _env_value(credential_file, "TV_OBSERVER_PASSWORD_HASH")
            if credential_file is not None
            else password_hash
        )

    @app.before_request
    def protect_request() -> BaseResponse | None:
        remote = request.remote_addr or ""
        if not allow_lan and not _valid_cidr(remote, ("127.0.0.0/8", "::1/128")):
            abort(403)
        if allow_lan and not _valid_cidr(remote, trusted_cidrs):
            abort(403)
        if request.method == "POST":
            supplied = request.form.get("csrf_token", "")
            expected = session.get("csrf_token", "")
            if (
                not isinstance(expected, str)
                or not expected
                or not supplied
                or not hmac.compare_digest(supplied, expected)
            ):
                abort(400, "invalid CSRF token")
        if request.endpoint not in {
            "login",
            "login_form",
            "static",
            "suite_ui.static",
        } and not session.get("authenticated"):
            return redirect(url_for("login"))
        return None

    @app.get("/login")
    def login_form() -> str | BaseResponse:
        if session.get("authenticated"):
            return redirect(url_for("index"))
        return render_template("login.html")

    @app.post("/login")
    def login() -> BaseResponse:
        key = f"login:{request.remote_addr}"
        if not limiter.allow(key):
            abort(429)
        if not check_password_hash(current_password_hash(), request.form.get("password", "")):
            flash("Invalid credentials", "error")
            return redirect(url_for("login_form"))
        session.clear()
        session["authenticated"] = True
        _csrf_token()
        return redirect(url_for("index"))

    @app.post("/logout")
    def logout() -> BaseResponse:
        session.clear()
        return redirect(url_for("login_form"))

    def render_section(active: str) -> str:
        state = _snapshot_state(inventory_root)
        suite_host = request.host.partition(":")[0]
        devices: list[dict[str, str]] = []
        connection_error = ""
        if active == "Connect":
            try:
                devices = device_reader()
            except (OSError, AdbError) as exc:
                connection_error = str(exc)
        return render_template(
            "dashboard.html",
            sections=SECTIONS,
            active=active,
            state=state,
            devices=devices,
            connection_error=connection_error,
            recovery=default_readiness_report(),
            settings={
                "observer_url": f"http://{suite_host}:8090",
                "controller_url": f"http://{suite_host}:8091",
                "bind": "0.0.0.0",  # noqa: S104 - informational LAN bind value.
                "trusted_networks": "10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16",
                "snapshot_root": str(inventory_root),
                "data_root": str(data_root),
                "password_change_available": credential_file is not None,
            },
        )

    @app.get("/")
    def index() -> str:
        return render_section("Overview")

    @app.get("/section/<name>")
    def section(name: str) -> str:
        normalized = name.replace("-", " ").lower()
        aliases = {
            "device": "overview",
            "connection": "connect",
            "firmware": "overview",
            "observation": "observe",
        }
        normalized = aliases.get(normalized, normalized)
        matches = [section_name for section_name in SECTIONS if section_name.lower() == normalized]
        if not matches:
            abort(404)
        return render_section(matches[0])

    @app.get("/status/partial")
    def partial_status() -> BaseResponse:
        state = _snapshot_state(inventory_root)
        snapshots = state["snapshots"]
        try:
            devices = device_reader()
        except (OSError, AdbError):
            devices = []
        return jsonify(
            {
                "connected_devices": devices,
                "snapshot_count": len(snapshots) if isinstance(snapshots, list) else 0,
            }
        )

    @app.post("/snapshot/capture")
    def snapshot_capture_route() -> BaseResponse:
        device_name = request.form.get("device_name", "").strip()
        serial = request.form.get("serial", "").strip()
        if not device_name or not serial:
            flash("Select an authorized TV and enter a local device label", "error")
        else:
            try:
                output = snapshot_capture(inventory_root, device_name, serial)
                flash(f"Verified read-only snapshot created: {output.name}", "success")
            except (OSError, AdbError, SnapshotError, ValueError) as exc:
                flash(f"Snapshot was not created: {exc}", "error")
        return redirect(url_for("section", name="connect"))

    def latest_packages() -> list[dict[str, object]]:
        latest = _snapshot_state(inventory_root)["latest"]
        if not isinstance(latest, dict) or not isinstance(latest.get("packages"), list):
            abort(404, "no verified application inventory")
        return [item for item in latest["packages"] if isinstance(item, dict)]

    @app.get("/exports/applications.json")
    def applications_json() -> Response:
        body = json.dumps(latest_packages(), indent=2, sort_keys=True) + "\n"
        return Response(
            body,
            mimetype="application/json",
            headers={"Content-Disposition": "attachment; filename=tv-applications.json"},
        )

    @app.get("/exports/applications.txt")
    def applications_text() -> Response:
        lines = ["package\tversion\tinstall_level\tscope\tstate\trequested_permissions"]
        for package in latest_packages():
            permissions = package.get("requested_permissions", [])
            permission_text = ",".join(str(item) for item in permissions) if isinstance(
                permissions, list
            ) else ""
            lines.append(
                "\t".join(
                    (
                        str(package.get("name", "unknown")),
                        str(package.get("version", "unknown")),
                        str(package.get("install_level", "unknown")),
                        str(package.get("scope", "unknown")),
                        "enabled" if package.get("enabled") is True else "disabled",
                        permission_text,
                    )
                )
            )
        return Response(
            "\n".join(lines) + "\n",
            mimetype="text/plain",
            headers={"Content-Disposition": "attachment; filename=tv-applications.txt"},
        )

    @app.post("/observation/start")
    def observation_start() -> BaseResponse:
        name = request.form.get("name", "").strip()
        if not name or len(name) > 100:
            flash("Enter a session name up to 100 characters", "error")
        else:
            flash("Observation request recorded", "success")
        return redirect(url_for("section", name="observe"))

    @app.post("/settings/password")
    def settings_password() -> BaseResponse:
        if credential_file is None:
            abort(503, "password management is unavailable in this runtime")
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirmation = request.form.get("confirm_password", "")
        if not check_password_hash(current_password_hash(), current):
            flash("Current password is incorrect", "error")
        elif len(new) < 6:
            flash("New password must contain at least 6 characters", "error")
        elif new != confirmation:
            flash("New passwords do not match", "error")
        else:
            from werkzeug.security import generate_password_hash

            _set_env_value(
                credential_file, "TV_OBSERVER_PASSWORD_HASH", generate_password_hash(new)
            )
            session.clear()
            flash("Shared Observer and Controller password changed", "success")
            return redirect(url_for("login_form"))
        return redirect(url_for("section", name="settings"))

    return app


AppFactory = Callable[..., Flask]
