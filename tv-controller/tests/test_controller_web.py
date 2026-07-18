from __future__ import annotations

import json
import re
from pathlib import Path

from test_controller_snapshot import digest, make_snapshot
from werkzeug.security import generate_password_hash

from tv_controller.apk_manager import ApkSettings, ApkSettingsStore, atomic_write_json
from tv_controller.service import SECTIONS, _package_rows, create_app
from tv_controller.state_history import DeviceIdentity, PackageState, ProfilePackage

PASSWORD = "123456"  # noqa: S105


def csrf(client) -> str:
    response = client.get("/login")
    match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
    assert match
    return match.group(1).decode()


def authenticated_client(app):
    app.config["TESTING"] = True
    client = app.test_client()
    client.post("/login", data={"csrf_token": csrf(client), "password": PASSWORD})
    return client


def test_shared_password_auth_and_sections(tmp_path: Path) -> None:
    app = create_app(
        secret_key="shared-test-secret",  # noqa: S106
        password_hash=generate_password_hash(PASSWORD),
        snapshot_root=tmp_path / "snapshots",
    )
    app.config["TESTING"] = True
    client = app.test_client()
    assert client.get("/").status_code == 302
    assert client.get("/suite-static/suite.css").status_code == 200
    response = client.post(
        "/login", data={"csrf_token": csrf(client), "password": PASSWORD}
    )
    assert response.status_code == 302
    for section in SECTIONS:
        page = client.get(f"/section/{section.lower().replace(' ', '-')}")
        assert page.status_code == 200
        assert section.encode() in page.data
    assert b"No verified snapshot yet" in client.get("/").data
    assert client.get("/section/applications").location == (
        "http://localhost:8090/section/applications"
    )
    assert client.get("/section/snapshots").location == "http://localhost:8090/section/snapshots"


def test_controller_lan_guard(tmp_path: Path) -> None:
    app = create_app(
        secret_key="shared-test-secret",  # noqa: S106
        password_hash=generate_password_hash(PASSWORD),
        snapshot_root=tmp_path,
        allow_lan=True,
        trusted_cidrs=("192.168.0.0/16",),
    )
    app.config["TESTING"] = True
    client = app.test_client()
    assert client.get("/login", environ_base={"REMOTE_ADDR": "192.168.1.2"}).status_code == 200
    assert client.get("/login", environ_base={"REMOTE_ADDR": "203.0.113.2"}).status_code == 403


def test_controller_reads_shared_password_file_on_each_login(tmp_path: Path) -> None:
    credential_file = tmp_path / "observer.env"
    credential_file.write_text(
        f"TV_OBSERVER_PASSWORD_HASH={generate_password_hash('654321')}\n", encoding="utf-8"
    )
    app = create_app(
        secret_key="shared-test-secret",  # noqa: S106
        password_hash="unused",  # noqa: S106
        credential_file=credential_file,
        snapshot_root=tmp_path / "snapshots",
    )
    app.config["TESTING"] = True
    client = app.test_client()
    response = client.post(
        "/login", data={"csrf_token": csrf(client), "password": "654321"}
    )
    assert response.status_code == 302


def test_application_inventory_separates_user_and_protected_packages(tmp_path: Path) -> None:
    snapshot = make_snapshot(tmp_path)
    packages = [
        {"name": "com.example.streamer", "version": "1", "install_level": "user"},
        {"name": "com.vendor.core", "version": "2", "install_level": "system"},
        {"name": "com.android.systemui", "version": "3", "install_level": "system"},
    ]
    package_file = snapshot / "packages.json"
    package_file.write_text(json.dumps(packages), encoding="utf-8")
    with (snapshot / "checksums.sha256").open("a", encoding="ascii") as checksums:
        checksums.write(f"{digest(package_file)}  packages.json\n")
    rows = _package_rows(str(snapshot))
    categories = {row["name"]: row["category"] for row in rows}
    assert categories == {
        "com.android.systemui": "critical-protected",
        "com.example.streamer": "user-review",
        "com.vendor.core": "system-protected",
    }
    assert {row["action"] for row in rows} == {"Not enabled"}


def test_apk_manager_live_api_and_settings(tmp_path: Path) -> None:
    settings_path = tmp_path / "controller-settings.json"
    app = create_app(
        secret_key="shared-test-secret",  # noqa: S106
        password_hash=generate_password_hash(PASSWORD),
        snapshot_root=tmp_path / "snapshots",
        apk_root=tmp_path / "apks",
        controller_settings=settings_path,
    )
    client = authenticated_client(app)
    page = client.get("/section/apk-manager")
    assert page.status_code == 200
    assert b"Downloaded APKs" in page.data
    live = client.get("/api/live").get_json()
    assert live["apk_table"] == []
    assert live["apk_tasks"] == []
    assert live["sideload_enabled"] is False
    assert live["adb_host"] == "192.168.0.110:5555"

    token_match = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
    assert token_match
    response = client.post(
        "/settings/controller",
        data={
            "csrf_token": token_match.group(1).decode(),
            "downloader_endpoint": (
                "https://online-apk-downloader.com/apk-ajax&packageDownload"
            ),
            "adb_host": "192.168.0.110",
            "sideload_enabled": "true",
        },
    )
    assert response.status_code == 302
    saved = json.loads(settings_path.read_text(encoding="utf-8"))
    assert saved["adb_host"] == "192.168.0.110:5555"
    assert saved["sideload_enabled"] is True
    assert settings_path.stat().st_mode & 0o777 == 0o600


def test_invalid_apk_download_input_is_rejected_without_background_job(tmp_path: Path) -> None:
    app = create_app(
        secret_key="shared-test-secret",  # noqa: S106
        password_hash=generate_password_hash(PASSWORD),
        snapshot_root=tmp_path / "snapshots",
        apk_root=tmp_path / "apks",
    )
    client = authenticated_client(app)
    page = client.get("/section/apk-manager")
    token_match = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
    assert token_match
    response = client.post(
        "/apk/download",
        data={"csrf_token": token_match.group(1).decode(), "packages": "../not-a-package"},
        follow_redirects=True,
    )
    assert b"Invalid Android package ID" in response.data
    live = client.get("/api/live").get_json()
    assert live["apk_table"] == []
    assert live["apk_tasks"] == []


def test_locked_apk_manager_hides_tv_mutations_but_keeps_read_only_check(
    tmp_path: Path, monkeypatch
) -> None:
    apk_root = tmp_path / "apks"
    apk_root.mkdir()
    apk = apk_root / "de.zalando.mobile-42.apk"
    apk.write_bytes(b"test fixture")
    atomic_write_json(
        apk.with_name(f"{apk.name}.json"),
        {
            "package": "de.zalando.mobile",
            "file": apk.name,
            "versionCode": 42,
            "versionSource": "test",
        },
    )
    app = create_app(
        secret_key="shared-test-secret",  # noqa: S106
        password_hash=generate_password_hash(PASSWORD),
        snapshot_root=tmp_path / "snapshots",
        apk_root=apk_root,
        controller_settings=tmp_path / "settings.json",
    )
    client = authenticated_client(app)
    page = client.get("/section/apk-manager")
    assert b"TV actions locked" in page.data
    assert b'data-kind="check"' in page.data
    assert b'data-kind="install"' not in page.data
    assert b'data-kind="update"' not in page.data
    assert b'data-kind="uninstall"' not in page.data

    manager = app.extensions["apk_jobs"]
    monkeypatch.setattr(manager, "start_check_update", lambda package: "test-task")
    token_match = re.search(rb'data-csrf="([^"]+)"', page.data)
    assert token_match
    response = client.post(
        "/apk/check-update",
        data={
            "csrf_token": token_match.group(1).decode(),
            "package": "de.zalando.mobile",
        },
        headers={"Accept": "application/json", "X-Requested-With": "fetch"},
    )
    assert response.status_code == 202
    assert response.get_json() == {
        "message": "Update check started.",
        "ok": True,
        "task_id": "test-task",
    }


def test_tv_preflight_and_finished_task_cleanup_endpoints(tmp_path: Path, monkeypatch) -> None:
    app = create_app(
        secret_key="shared-test-secret",  # noqa: S106
        password_hash=generate_password_hash(PASSWORD),
        snapshot_root=tmp_path / "snapshots",
        apk_root=tmp_path / "apks",
    )
    client = authenticated_client(app)
    page = client.get("/section/apk-manager")
    token_match = re.search(rb'data-csrf="([^"]+)"', page.data)
    assert token_match
    manager = app.extensions["apk_jobs"]
    monkeypatch.setattr(
        manager,
        "check_tv",
        lambda: type("ReadyAdb", (), {"host": "192.168.0.110:5555"})(),
    )
    token = token_match.group(1).decode()
    preflight = client.post("/apk/preflight", data={"csrf_token": token})
    assert preflight.status_code == 200
    assert preflight.get_json()["message"] == "TV is online and ADB is authorized."
    cleanup = client.post("/apk/tasks/clear", data={"csrf_token": token})
    assert cleanup.status_code == 200
    assert cleanup.get_json()["removed"] == 0


def test_enabled_apk_manager_renders_confirmed_tv_actions(tmp_path: Path) -> None:
    apk_root = tmp_path / "apks"
    apk_root.mkdir()
    apk = apk_root / "de.zalando.mobile-42.apk"
    apk.write_bytes(b"test fixture")
    atomic_write_json(
        apk.with_name(f"{apk.name}.json"),
        {"package": "de.zalando.mobile", "file": apk.name, "versionCode": 42},
    )
    settings_path = tmp_path / "settings.json"
    ApkSettingsStore(settings_path).save(ApkSettings(sideload_enabled=True))
    app = create_app(
        secret_key="shared-test-secret",  # noqa: S106
        password_hash=generate_password_hash(PASSWORD),
        snapshot_root=tmp_path / "snapshots",
        apk_root=apk_root,
        controller_settings=settings_path,
    )
    page = authenticated_client(app).get("/section/apk-manager")
    assert b'data-kind="install"' in page.data
    assert b'data-kind="update"' in page.data
    assert b'data-kind="uninstall"' in page.data


def test_journal_renders_selective_rollback_and_routes_exact_operation(
    tmp_path: Path, monkeypatch
) -> None:
    settings_path = tmp_path / "settings.json"
    ApkSettingsStore(settings_path).save(ApkSettings(sideload_enabled=True))
    app = create_app(
        secret_key="shared-test-secret",  # noqa: S106
        password_hash=generate_password_hash(PASSWORD),
        snapshot_root=tmp_path / "snapshots",
        apk_root=tmp_path / "apks",
        controller_settings=settings_path,
    )
    manager = app.extensions["apk_jobs"]
    device = DeviceIdentity("TV", "android-tv", "build-1", "fingerprint")
    before = PackageState(True, True, True, 1, "1")
    after = PackageState(True, False, True, 1, "1")
    operation_id = manager.journal.begin(
        package="app.one", action="disable", before=before, device=device
    )
    manager.journal.complete(operation_id, after, "enable")
    monkeypatch.setattr(manager, "start_rollback", lambda value: f"rollback-{value}")
    client = authenticated_client(app)
    page = client.get("/section/journal")
    assert b"app.one" in page.data
    assert f"rollback-{operation_id}".encode() in page.data
    token = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
    assert token
    response = client.post(
        "/journal/rollback",
        data={
            "csrf_token": token.group(1).decode(),
            "operation_id": str(operation_id),
            "confirmation": f"rollback-{operation_id}",
        },
        headers={"Accept": "application/json", "X-Requested-With": "fetch"},
    )
    assert response.status_code == 202
    assert response.get_json()["task_id"] == f"rollback-{operation_id}"


def test_profile_preview_and_typed_apply_confirmation(tmp_path: Path, monkeypatch) -> None:
    app = create_app(
        secret_key="shared-test-secret",  # noqa: S106
        password_hash=generate_password_hash(PASSWORD),
        snapshot_root=tmp_path / "snapshots",
        apk_root=tmp_path / "apks",
    )
    manager = app.extensions["apk_jobs"]
    profile = manager.profiles.save(
        "Stable",
        DeviceIdentity("TV", "android-tv", "build-1", "fingerprint"),
        [ProfilePackage("app.one", True, 1, "1", "app.one-1.apk")],
    )
    monkeypatch.setattr(
        manager,
        "profile_preview",
        lambda _: {
            "profile": profile,
            "operations": [{"package": "app.one", "action": "install"}],
            "blockers": [],
            "ready": True,
        },
    )
    monkeypatch.setattr(manager, "start_profile_apply", lambda _: "profile-task")
    client = authenticated_client(app)
    page = client.get(f"/section/plans?profile={profile.id}")
    assert b"Apply exact profile" in page.data
    token = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
    assert token
    rejected = client.post(
        "/profiles/apply",
        data={
            "csrf_token": token.group(1).decode(),
            "profile_id": profile.id,
            "confirmation": "wrong",
        },
        headers={"Accept": "application/json", "X-Requested-With": "fetch"},
    )
    assert rejected.status_code == 400
    accepted = client.post(
        "/profiles/apply",
        data={
            "csrf_token": token.group(1).decode(),
            "profile_id": profile.id,
            "confirmation": "Stable",
        },
        headers={"Accept": "application/json", "X-Requested-With": "fetch"},
    )
    assert accepted.status_code == 202
