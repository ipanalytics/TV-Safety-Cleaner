from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
from tv_safety_shared import install_shared_ui
from werkzeug.security import check_password_hash
from werkzeug.wrappers import Response as BaseResponse

from tv_controller.apk_manager import (
    ApkJobManager,
    ApkRepository,
    ApkSettings,
    ApkSettingsStore,
    parse_package_inputs,
    task_error_message,
    validate_adb_host,
    validate_downloader_endpoint,
)
from tv_controller.planner import is_never_touch
from tv_controller.snapshot import ControllerRefusal, load_snapshot

SECTIONS = (
    "Overview",
    "APK Manager",
    "Plans",
    "Safety",
    "Journal",
    "Settings",
    "Diagnostics",
)


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


def _env_value(path: Path, key: str) -> str:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("credential file is unavailable")
    prefix = f"{key}="
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return line.removeprefix(prefix)
    raise RuntimeError(f"credential value is missing: {key}")


def _snapshot_rows(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not root.exists():
        return rows
    for checksums in root.glob("*/*/checksums.sha256"):
        try:
            snapshot = load_snapshot(checksums.parent)
        except (OSError, ControllerRefusal):
            continue
        rows.append(
            {
                "path": str(snapshot.path),
                "device": snapshot.path.parent.name,
                "created": snapshot.manifest.get("created_local", "unknown"),
                "model": snapshot.manifest.get("model", "unknown"),
                "platform": snapshot.manifest.get("platform", "unknown"),
                "firmware": snapshot.manifest.get("firmware", "unknown"),
                "packages": snapshot.manifest.get("package_count", 0),
                "fingerprint": snapshot.manifest.get("fingerprint", "unknown"),
            }
        )
    return sorted(rows, key=lambda row: str(row["created"]), reverse=True)


def _package_rows(snapshot_path: str | None) -> list[dict[str, object]]:
    if snapshot_path is None:
        return []
    path = Path(snapshot_path)
    try:
        snapshot = load_snapshot(path)
        raw = json.loads((snapshot.path / "packages.json").read_text(encoding="utf-8"))
    except (OSError, ControllerRefusal, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "unknown"))
        level = str(item.get("install_level", "unknown"))
        if is_never_touch(name):
            category = "critical-protected"
            assessment = "Never touch"
        elif level == "user":
            category = "user-review"
            assessment = "User-level candidate for review"
        elif level in {"system", "updated-system"}:
            category = "system-protected"
            assessment = "System information only"
        else:
            category = "unknown"
            assessment = "Unknown; no action"
        rows.append(
            {
                "name": name,
                "version": str(item.get("version", "unknown")),
                "install_level": level,
                "enabled": item.get("enabled") is True,
                "permissions": item.get("requested_permissions", []),
                "category": category,
                "assessment": assessment,
                "action": "Not enabled",
            }
        )
    return sorted(rows, key=lambda row: (str(row["category"]), str(row["name"])))


def create_app(
    *,
    secret_key: str,
    password_hash: str,
    snapshot_root: Path,
    apk_root: Path | None = None,
    controller_settings: Path | None = None,
    credential_file: Path | None = None,
    allow_lan: bool = False,
    trusted_cidrs: tuple[str, ...] = ("127.0.0.0/8", "::1/128"),
) -> Flask:
    if allow_lan and not trusted_cidrs:
        raise ValueError("LAN access requires at least one trusted CIDR")
    app = Flask(__name__)
    install_shared_ui(app)
    app.config.update(
        SECRET_KEY=secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
    )
    app.jinja_env.globals["csrf_token"] = _csrf_token
    app.jinja_env.globals["format_bytes"] = _format_bytes
    app.jinja_env.globals["journal_error"] = task_error_message
    limiter = RateLimiter()
    resolved_apk_root = apk_root or snapshot_root.parent / "controller-apks"
    resolved_settings = controller_settings or resolved_apk_root.parent / "controller-settings.json"
    repository = ApkRepository(resolved_apk_root)
    settings_store = ApkSettingsStore(resolved_settings)
    apk_jobs = ApkJobManager(repository, settings_store)
    app.extensions["apk_repository"] = repository
    app.extensions["apk_settings"] = settings_store
    app.extensions["apk_jobs"] = apk_jobs

    log_path = resolved_apk_root.parent / "apk-manager.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not app.testing:
        handler = logging.FileHandler(log_path)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger("tv_controller.apk_manager").addHandler(handler)

    def current_password_hash() -> str:
        return (
            _env_value(credential_file, "TV_OBSERVER_PASSWORD_HASH")
            if credential_file is not None
            else password_hash
        )

    @app.before_request
    def protect_request() -> BaseResponse | None:
        remote = request.remote_addr or ""
        allowed = trusted_cidrs if allow_lan else ("127.0.0.0/8", "::1/128")
        if not _valid_cidr(remote, allowed):
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
            return redirect(url_for("login_form"))
        return None

    @app.get("/login")
    def login_form() -> str | BaseResponse:
        if session.get("authenticated"):
            return redirect(url_for("index"))
        return render_template("login.html")

    @app.post("/login")
    def login() -> BaseResponse:
        if not limiter.allow(f"login:{request.remote_addr}"):
            abort(429)
        if not check_password_hash(current_password_hash(), request.form.get("password", "")):
            return redirect(url_for("login_form", error="invalid"))
        session.clear()
        session["authenticated"] = True
        _csrf_token()
        return redirect(url_for("index"))

    @app.post("/logout")
    def logout() -> BaseResponse:
        session.clear()
        return redirect(url_for("login_form"))

    def render_section(active: str) -> str:
        snapshots = _snapshot_rows(snapshot_root)
        latest = snapshots[0] if snapshots else None
        profile_preview: dict[str, Any] | None = None
        profile_error = ""
        selected_profile = request.args.get("profile", "") if active == "Plans" else ""
        if selected_profile:
            try:
                profile_preview = apk_jobs.profile_preview(selected_profile)
            except ControllerRefusal as exc:
                profile_error = str(exc)
        return render_template(
            "dashboard.html",
            sections=SECTIONS,
            active=active,
            snapshots=snapshots,
            latest=latest,
            apk_rows=repository.list_apks(),
            apk_tasks=apk_jobs.live()["apk_tasks"],
            apk_settings=settings_store.load(),
            operation_history=apk_jobs.journal.list(),
            state_profiles=apk_jobs.profiles.list(),
            profile_preview=profile_preview,
            profile_error=profile_error,
        )

    @app.get("/")
    def index() -> str:
        return render_section("Overview")

    @app.get("/section/<name>")
    def section(name: str) -> str | BaseResponse:
        normalized = name.replace("-", " ").lower()
        if normalized in {"applications", "snapshots"}:
            host = request.host.partition(":")[0]
            return redirect(f"{request.scheme}://{host}:8090/section/{normalized}")
        matches = [item for item in SECTIONS if item.lower() == normalized]
        if not matches:
            abort(404)
        return render_section(matches[0])

    @app.get("/api/live")
    def live() -> BaseResponse:
        return jsonify(apk_jobs.live())

    @app.post("/apk/preflight")
    def apk_preflight() -> BaseResponse:
        try:
            client = apk_jobs.check_tv()
            return jsonify(
                {
                    "ok": True,
                    "message": "TV is online and ADB is authorized.",
                    "adb_host": client.host,
                }
            )
        except ControllerRefusal as exc:
            response = jsonify({"ok": False, "message": str(exc)})
            response.status_code = 409
            return response

    @app.post("/apk/tasks/clear")
    def apk_tasks_clear() -> BaseResponse:
        removed = apk_jobs.clear_finished()
        return jsonify(
            {
                "ok": True,
                "message": f"Cleared {removed} completed or failed operation(s).",
                "removed": removed,
            }
        )

    def action_response(
        message: str,
        *,
        status: int = 200,
        category: str = "info",
        task_id: str | None = None,
        fragment: str = "task-heading",
    ) -> BaseResponse:
        wants_json = (
            request.headers.get("X-Requested-With") == "fetch"
            or request.accept_mimetypes.best == "application/json"
        )
        if wants_json:
            response = jsonify(
                {
                    "ok": status < 400,
                    "message": message,
                    "task_id": task_id,
                }
            )
            response.status_code = status
            return response
        flash(message, category)
        location = f"{url_for('section', name='apk-manager')}#{fragment}"
        return redirect(location)

    @app.post("/apk/download")
    def apk_download() -> BaseResponse:
        try:
            packages = parse_package_inputs(request.form.get("packages", ""))
            task_ids = []
            for package in packages:
                task_ids.append(apk_jobs.start_download(package))
            return action_response(
                f"APK downloads started: {len(packages)}",
                status=202,
                task_id=task_ids[0] if task_ids else None,
            )
        except (OSError, ValueError, ControllerRefusal) as exc:
            return action_response(str(exc), status=400, category="error")

    @app.post("/apk/delete")
    def apk_delete() -> BaseResponse:
        try:
            if request.form.get("confirmation") != "delete-local-apk":
                raise ControllerRefusal("Confirm local APK deletion")
            apk_jobs.delete_local_apk(request.form.get("file", ""))
            return action_response(
                "Local APK deleted. The TV was not changed.", fragment="apk-table-heading"
            )
        except (OSError, ValueError, ControllerRefusal) as exc:
            return action_response(str(exc), status=400, category="error")

    @app.post("/apk/install")
    def apk_install() -> BaseResponse:
        try:
            if request.form.get("confirmation") != "install-apk":
                raise ControllerRefusal("Confirm APK installation on the TV")
            task_id = apk_jobs.start_install(request.form.get("file", ""))
            return action_response("APK installation queued.", status=202, task_id=task_id)
        except (OSError, ValueError, ControllerRefusal) as exc:
            return action_response(str(exc), status=400, category="error")

    @app.post("/apk/uninstall")
    def apk_uninstall() -> BaseResponse:
        try:
            if request.form.get("confirmation") != "uninstall-user-app":
                raise ControllerRefusal("Confirm third-party application removal from the TV")
            task_id = apk_jobs.start_uninstall(request.form.get("package", ""))
            return action_response(
                "Third-party application uninstall queued.", status=202, task_id=task_id
            )
        except (OSError, ValueError, ControllerRefusal) as exc:
            return action_response(str(exc), status=400, category="error")

    @app.post("/apk/check-update")
    def apk_check_update() -> BaseResponse:
        try:
            task_id = apk_jobs.start_check_update(request.form.get("package", ""))
            return action_response("Update check started.", status=202, task_id=task_id)
        except (OSError, ValueError, ControllerRefusal) as exc:
            return action_response(str(exc), status=400, category="error")

    @app.post("/apk/update")
    def apk_update() -> BaseResponse:
        try:
            if request.form.get("confirmation") != "update-apk":
                raise ControllerRefusal("Confirm APK update on the TV")
            task_id = apk_jobs.start_update(request.form.get("package", ""))
            return action_response(
                "APK download and update queued.", status=202, task_id=task_id
            )
        except (OSError, ValueError, ControllerRefusal) as exc:
            return action_response(str(exc), status=400, category="error")

    @app.post("/packages/state")
    def package_state_change() -> BaseResponse:
        package = request.form.get("package", "").strip()
        action = request.form.get("action", "")
        try:
            if action not in {"enable", "disable"}:
                raise ControllerRefusal("Package state action is invalid")
            if request.form.get("confirmation") != "change-user-package-state":
                raise ControllerRefusal("Confirm the user-package state change")
            task_id = apk_jobs.start_set_enabled(package, enabled=action == "enable")
            return action_response(
                f"Package {action} queued.", status=202, task_id=task_id
            )
        except (OSError, ValueError, ControllerRefusal) as exc:
            return action_response(str(exc), status=400, category="error")

    @app.post("/journal/rollback")
    def journal_rollback() -> BaseResponse:
        try:
            operation_id = int(request.form.get("operation_id", "0"))
            if request.form.get("confirmation") != f"rollback-{operation_id}":
                raise ControllerRefusal("Confirm the selected rollback")
            task_id = apk_jobs.start_rollback(operation_id)
            return action_response(
                "Selective rollback queued.", status=202, task_id=task_id
            )
        except (OSError, ValueError, ControllerRefusal) as exc:
            return action_response(str(exc), status=400, category="error")

    @app.post("/profiles/capture")
    def profile_capture() -> BaseResponse:
        try:
            task_id = apk_jobs.start_profile_capture(request.form.get("name", ""))
            return action_response(
                "Profile capture started.", status=202, task_id=task_id
            )
        except (OSError, ValueError, ControllerRefusal) as exc:
            return action_response(str(exc), status=400, category="error")

    @app.post("/profiles/apply")
    def profile_apply() -> BaseResponse:
        try:
            profile_id = request.form.get("profile_id", "")
            profile = apk_jobs.profiles.get(profile_id)
            if request.form.get("confirmation", "").strip() != profile.name:
                raise ControllerRefusal("Type the exact profile name to confirm")
            task_id = apk_jobs.start_profile_apply(profile_id)
            return action_response(
                "Profile application queued.", status=202, task_id=task_id
            )
        except (OSError, ValueError, ControllerRefusal) as exc:
            return action_response(str(exc), status=400, category="error")

    @app.post("/settings/controller")
    def save_controller_settings() -> BaseResponse:
        try:
            settings_store.save(
                ApkSettings(
                    downloader_endpoint=validate_downloader_endpoint(
                        request.form.get("downloader_endpoint", "")
                    ),
                    adb_host=validate_adb_host(request.form.get("adb_host", "")),
                    sideload_enabled=request.form.get("sideload_enabled") == "true",
                )
            )
            flash("Controller sideloading settings saved.", "info")
        except (OSError, ValueError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("section", name="settings"))

    return app


def _format_bytes(value: object) -> str:
    try:
        size = float(str(value))
    except (TypeError, ValueError):
        return "unknown"
    units = ("B", "KB", "MB", "GB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "unknown"
