from __future__ import annotations

import io
import json
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest

from tv_controller.apk_manager import (
    AdbClient,
    ApkDownloader,
    ApkJobManager,
    ApkRepository,
    ApkSettings,
    ApkSettingsStore,
    TaskStore,
    atomic_write_json,
    parse_package_input,
    safe_apk_filename,
    version_from_download_data,
)
from tv_controller.snapshot import ControllerRefusal
from tv_controller.state_history import DeviceIdentity, PackageState


def apk_bytes() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"manifest")
        archive.writestr("META-INF/RELEASE.RSA", b"signature")
    return output.getvalue()


def write_apk(
    root: Path, name: str, package: str | None = None, *, version_code: int = 42
) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(apk_bytes())
    if package:
        atomic_write_json(
            path.with_name(f"{path.name}.json"),
            {
                "package": package,
                "file": path.name,
                "versionCode": version_code,
                "versionSource": "test",
            },
        )
    return path


def wait_for_task(store: TaskStore, task_id: str, stages: set[str]) -> dict[str, Any]:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        task = store.read(task_id)
        if str(task.get("stage")) in stages:
            return task
        time.sleep(0.01)
    raise AssertionError(f"task did not reach {stages}: {store.read(task_id)}")


def test_package_and_google_play_parsing() -> None:
    assert parse_package_input("de.zalando.mobile") == "de.zalando.mobile"
    assert (
        parse_package_input("https://play.google.com/store/apps/details?id=de.zalando.mobile")
        == "de.zalando.mobile"
    )


@pytest.mark.parametrize("value", ["invalid", "../evil.apk", "https://example.com/?id=a.b"])
def test_invalid_package_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        parse_package_input(value)


def test_version_resolution_order() -> None:
    assert version_from_download_data({"version_code": "147022"}) == (
        147022,
        "downloader JSON field version_code",
    )
    assert version_from_download_data({}, "de.zalando.mobile-147021-merged.apk") == (
        147021,
        "downloader filename",
    )
    assert version_from_download_data({}, "de.zalando.mobile.apk") == (0, "unknown")


def test_safe_apk_filename_rejects_traversal() -> None:
    assert safe_apk_filename("app-1.apk") == "app-1.apk"
    with pytest.raises(ControllerRefusal):
        safe_apk_filename("../app.apk")


def test_task_json_is_atomic(tmp_path: Path) -> None:
    path = tmp_path / "task.json"
    atomic_write_json(path, {"stage": "done"})
    assert json.loads(path.read_text()) == {"stage": "done"}
    assert not list(tmp_path.glob("*.tmp"))


def test_poll_normalizes_escaped_download_url(tmp_path: Path) -> None:
    repository = ApkRepository(tmp_path / "apks")
    task_id = repository.tasks.new("de.zalando.mobile")
    downloader = ApkDownloader(
        repository,
        fetch_json=lambda _: {
            "success": True,
            "downloadUrl": "https:\\/\\/online-apk-downloader.com\\/core\\/app.apk",
        },
    )
    result = downloader._poll("de.zalando.mobile", task_id)  # noqa: SLF001
    assert result["downloadUrl"] == "https://online-apk-downloader.com/core/app.apk"


def test_failed_timeout_produces_failed_task(tmp_path: Path) -> None:
    repository = ApkRepository(tmp_path / "apks")
    current = [0.0]

    def sleep(seconds: float) -> None:
        current[0] += seconds

    downloader = ApkDownloader(
        repository,
        fetch_json=lambda _: {"success": False, "message": "Processing"},
        sleeper=sleep,
        clock=lambda: current[0],
    )
    task_id = repository.tasks.new("de.zalando.mobile")
    with pytest.raises(TimeoutError):
        downloader.download("de.zalando.mobile", task_id)
    task = repository.tasks.read(task_id)
    assert task["stage"] == "failed"
    assert "timeout" in task["error"]


def test_active_tasks_hide_done_and_expired_failed(tmp_path: Path) -> None:
    now = [1000.0]
    store = TaskStore(tmp_path, clock=lambda: now[0])
    done = store.new("a.b")
    store.update(done, stage="done")
    failed = store.new("c.d")
    store.update(failed, stage="failed")
    assert [task["id"] for task in store.active()] == [failed]
    now[0] += 301
    assert store.active() == []


def test_clear_finished_preserves_running_tasks(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    running = store.new("a.b")
    failed = store.new("c.d")
    result = store.new("e.f")
    store.update(failed, stage="failed")
    store.update(result, stage="result")
    assert store.clear_finished() == 2
    assert store.read(running)["stage"] == "queued"
    assert not (tmp_path / f"{failed}.json").exists()
    assert not (tmp_path / f"{result}.json").exists()


def test_live_hides_legacy_raw_adb_daemon_log(tmp_path: Path) -> None:
    repository = ApkRepository(tmp_path / "apks")
    manager = ApkJobManager(repository, ApkSettingsStore(tmp_path / "settings.json"))
    task_id = repository.tasks.new("tv.lfstrm.smotreshka", kind="install")
    repository.tasks.update(
        task_id,
        stage="failed",
        status="Failed",
        error=(
            "* daemon not running; starting now at tcp:5037\n"
            "ADB server didn't ACK\n"
            "Cannot mkdir '/srv/tv-safety-data/.android': Read-only file system"
        ),
    )

    task = manager.live()["apk_tasks"][0]
    assert task["detail"] == (
        "Controller could not start its local ADB service. Redeploy with tv.sh."
    )
    assert "Cannot mkdir" not in task["detail"]


def test_list_apks_reads_metadata_and_fallback(tmp_path: Path) -> None:
    repository = ApkRepository(tmp_path / "apks")
    write_apk(repository.root, "de.zalando.mobile-147021-merged.apk", "de.zalando.mobile")
    write_apk(repository.root, "org.videolan.vlc-301.apk")
    rows = {row["package"]: row for row in repository.list_apks()}
    assert rows["de.zalando.mobile"]["versionCode"] == 42
    assert rows["org.videolan.vlc"]["versionCode"] == 301


def test_delete_removes_apk_and_metadata_without_adb(tmp_path: Path) -> None:
    repository = ApkRepository(tmp_path / "apks")
    apk = write_apk(repository.root, "de.zalando.mobile-42.apk", "de.zalando.mobile")
    repository.delete(apk.name)
    assert not apk.exists()
    assert not apk.with_name(f"{apk.name}.json").exists()


class FakeAdb:
    installed = 10
    calls: list[tuple[str, str]] = []

    def __init__(self, host: str) -> None:
        self.host = host

    def preflight(self) -> None:
        pass

    def installed_version(self, package: str) -> int:
        self.calls.append(("version", package))
        return self.installed

    def device_identity(self) -> DeviceIdentity:
        return DeviceIdentity("TV", "android-tv", "build-1", "vendor/tv/build-1")

    def package_state(self, package: str) -> PackageState:
        return PackageState(True, True, True, self.installed, "1.0")

    def install(self, apk: Path, package: str) -> str:
        self.calls.append(("install", package))
        return "Success"

    def uninstall(self, package: str) -> str:
        self.calls.append(("uninstall", package))
        return "Success"


class FakeDownloader:
    def __init__(self, repository: ApkRepository, available: int = 11) -> None:
        self.repository = repository
        self.available = available

    def available_version(
        self, package: str, task_id: str
    ) -> tuple[int, str, dict[str, Any]]:
        return self.available, "test", {"downloadUrl": "https://example.invalid/app.apk"}

    def download(self, package: str, task_id: str | None = None) -> dict[str, Any]:
        apk = write_apk(self.repository.root, f"{package}-11.apk", package)
        return {"file": apk.name, "package": package}


def enabled_settings(path: Path) -> ApkSettingsStore:
    store = ApkSettingsStore(path)
    store.save(ApkSettings(sideload_enabled=True))
    return store


def test_check_update_compares_installed_and_available(tmp_path: Path) -> None:
    FakeAdb.calls = []
    repository = ApkRepository(tmp_path / "apks")
    manager = ApkJobManager(
        repository,
        enabled_settings(tmp_path / "settings.json"),
        downloader_factory=lambda _: FakeDownloader(repository),  # type: ignore[arg-type]
        adb_factory=FakeAdb,  # type: ignore[arg-type]
    )
    task_id = manager.start_check_update("de.zalando.mobile")
    task = wait_for_task(repository.tasks, task_id, {"result", "failed"})
    assert task["updateAvailable"] is True
    assert task["installedVersionCode"] == 10
    assert task["availableVersionCode"] == 11
    assert "Installed version code 10" in manager.live()["apk_tasks"][0]["detail"]


def test_update_downloads_then_installs(tmp_path: Path) -> None:
    FakeAdb.calls = []
    repository = ApkRepository(tmp_path / "apks")
    write_apk(
        repository.root,
        "de.zalando.mobile-10.apk",
        "de.zalando.mobile",
        version_code=10,
    )
    manager = ApkJobManager(
        repository,
        enabled_settings(tmp_path / "settings.json"),
        downloader_factory=lambda _: FakeDownloader(repository),  # type: ignore[arg-type]
        adb_factory=FakeAdb,  # type: ignore[arg-type]
    )
    task_id = manager.start_update("de.zalando.mobile")
    task = wait_for_task(repository.tasks, task_id, {"result", "failed"})
    assert task["stage"] == "result"
    assert FakeAdb.calls == [("install", "de.zalando.mobile")]


def test_read_only_update_check_works_while_sideloading_is_locked(tmp_path: Path) -> None:
    FakeAdb.calls = []
    repository = ApkRepository(tmp_path / "apks")
    manager = ApkJobManager(
        repository,
        ApkSettingsStore(tmp_path / "locked-settings.json"),
        downloader_factory=lambda _: FakeDownloader(repository),  # type: ignore[arg-type]
        adb_factory=FakeAdb,  # type: ignore[arg-type]
    )
    task_id = manager.start_check_update("de.zalando.mobile")
    task = wait_for_task(repository.tasks, task_id, {"result", "failed"})
    assert task["stage"] == "result"
    assert task["updateAvailable"] is True


def test_adb_refuses_never_touch_before_subprocess() -> None:
    with pytest.raises(ControllerRefusal, match="never-touch"):
        AdbClient("192.168.0.110").install(Path("unused.apk"), "com.android.systemui")


def test_adb_timeout_is_reported_without_internal_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd=["adb", "connect"], timeout=15)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(ControllerRefusal) as error:
        AdbClient("192.168.0.110").connect()
    assert str(error.value) == (
        "ADB connection timed out for 192.168.0.110:5555. "
        "Enable ADB and authorize this Controller."
    )


def test_adb_preflight_refuses_unreachable_tv_before_starting_adb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreachable(*args: object, **kwargs: object) -> None:
        raise ConnectionRefusedError

    def must_not_run(*args: object, **kwargs: object) -> None:
        pytest.fail("ADB must not start when the TV port is closed")

    monkeypatch.setattr("socket.create_connection", unreachable)
    monkeypatch.setattr(subprocess, "run", must_not_run)
    with pytest.raises(ControllerRefusal, match="TV is not reachable"):
        AdbClient("192.168.0.110").preflight()


def test_manager_does_not_create_task_when_tv_preflight_fails(tmp_path: Path) -> None:
    class UnavailableAdb(FakeAdb):
        def preflight(self) -> None:
            raise ControllerRefusal("TV is not reachable")

    repository = ApkRepository(tmp_path / "apks")
    manager = ApkJobManager(
        repository,
        ApkSettingsStore(tmp_path / "settings.json"),
        adb_factory=UnavailableAdb,  # type: ignore[arg-type]
    )
    with pytest.raises(ControllerRefusal, match="TV is not reachable"):
        manager.start_check_update("de.zalando.mobile")
    assert repository.tasks.active() == []
