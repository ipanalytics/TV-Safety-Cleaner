from __future__ import annotations

import time
from pathlib import Path

import pytest

from tv_controller.apk_manager import (
    ApkJobManager,
    ApkRepository,
    ApkSettings,
    ApkSettingsStore,
    atomic_write_json,
)
from tv_controller.snapshot import ControllerRefusal
from tv_controller.state_history import (
    DeviceIdentity,
    PackageState,
    ProfilePackage,
    StateJournal,
    StateProfileStore,
)

DEVICE = DeviceIdentity("TV", "android-tv", "build-1", "vendor/tv/build-1")
ABSENT = PackageState(False, False, False)
INSTALLED = PackageState(True, True, True, 10, "1.0")
DISABLED = PackageState(True, False, True, 10, "1.0")


class StatefulAdb:
    states: dict[str, PackageState] = {}

    def __init__(self, host: str) -> None:
        self.host = host

    def preflight(self) -> None:
        pass

    def device_identity(self) -> DeviceIdentity:
        return DEVICE

    def package_state(self, package: str) -> PackageState:
        return self.states.get(package, ABSENT)

    def third_party_packages(self) -> list[str]:
        return sorted(package for package, state in self.states.items() if state.installed)

    def install(self, apk: Path, package: str) -> str:
        version = int(apk.stem.rsplit("-", 1)[-1])
        self.states[package] = PackageState(True, True, True, version, str(version))
        return "Success"

    def uninstall(self, package: str) -> str:
        self.states[package] = ABSENT
        return "Success"

    def set_enabled(self, package: str, enabled: bool) -> str:
        before = self.states[package]
        self.states[package] = PackageState(
            before.installed,
            enabled,
            before.third_party,
            before.version_code,
            before.version_name,
        )
        return "Success"


def manager(tmp_path: Path) -> ApkJobManager:
    repository = ApkRepository(tmp_path / "apks")
    settings = ApkSettingsStore(tmp_path / "settings.json")
    settings.save(ApkSettings(sideload_enabled=True))
    return ApkJobManager(repository, settings, adb_factory=StatefulAdb)  # type: ignore[arg-type]


def archive(repository: ApkRepository, package: str, version: int) -> str:
    filename = f"{package}-{version}.apk"
    path = repository.root / filename
    path.write_bytes(b"fixture")
    atomic_write_json(
        path.with_name(f"{filename}.json"),
        {"package": package, "versionCode": version, "file": filename},
    )
    return filename


def wait(manager: ApkJobManager, task_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        task = manager.repository.tasks.read(task_id)
        if task.get("stage") in {"result", "failed"}:
            return task
        time.sleep(0.01)
    raise AssertionError("task did not finish")


def applied(journal: StateJournal, package: str, before: PackageState, after: PackageState) -> int:
    operation_id = journal.begin(
        package=package,
        action="disable" if after == DISABLED else "install",
        before=before,
        device=DEVICE,
    )
    journal.complete(operation_id, after, "enable" if after == DISABLED else "uninstall")
    return operation_id


def test_selective_rollback_ignores_later_operations_on_other_packages(tmp_path: Path) -> None:
    journal = StateJournal(tmp_path / "state.sqlite3")
    middle = applied(journal, "app.two", INSTALLED, DISABLED)
    applied(journal, "app.three", ABSENT, INSTALLED)
    record = journal.prepare_selective_rollback(middle, DISABLED, DEVICE)
    assert record.package == "app.two"
    assert record.inverse_action == "enable"


def test_selective_rollback_blocks_newer_same_package_and_external_drift(tmp_path: Path) -> None:
    journal = StateJournal(tmp_path / "state.sqlite3")
    first = applied(journal, "app.two", INSTALLED, DISABLED)
    applied(journal, "app.two", DISABLED, INSTALLED)
    with pytest.raises(ControllerRefusal, match="newer active operation"):
        journal.prepare_selective_rollback(first, INSTALLED, DEVICE)

    clean = StateJournal(tmp_path / "clean.sqlite3")
    operation_id = applied(clean, "app.two", INSTALLED, DISABLED)
    with pytest.raises(ControllerRefusal, match="current package state differs"):
        clean.prepare_selective_rollback(operation_id, INSTALLED, DEVICE)


def test_uncertain_operation_keeps_inverse_for_recovery(tmp_path: Path) -> None:
    journal = StateJournal(tmp_path / "state.sqlite3")
    operation_id = journal.begin(
        package="app.two",
        action="disable",
        before=INSTALLED,
        device=DEVICE,
        inverse_action="enable",
    )
    journal.fail(operation_id, "post-state read failed")
    record = journal.prepare_selective_rollback(operation_id, DISABLED, DEVICE)
    assert record.status == "uncertain"
    assert record.inverse_action == "enable"


def test_uncertain_no_change_can_be_reconciled(tmp_path: Path) -> None:
    journal = StateJournal(tmp_path / "state.sqlite3")
    operation_id = journal.begin(
        package="app.two",
        action="disable",
        before=INSTALLED,
        device=DEVICE,
        inverse_action="enable",
    )
    journal.fail(operation_id, "ADB result unknown")

    record = journal.prepare_selective_rollback(operation_id, INSTALLED, DEVICE)
    assert record.status == "uncertain"
    journal.reconcile_no_change(operation_id)
    assert journal.get(operation_id).status == "reconciled"


def test_newer_uncertain_operation_blocks_older_same_package_rollback(
    tmp_path: Path,
) -> None:
    journal = StateJournal(tmp_path / "state.sqlite3")
    first = applied(journal, "app.two", INSTALLED, DISABLED)
    newer = journal.begin(
        package="app.two",
        action="enable",
        before=DISABLED,
        device=DEVICE,
        inverse_action="disable",
    )
    journal.fail(newer, "post-state read failed")

    with pytest.raises(ControllerRefusal, match="newer active operation"):
        journal.prepare_selective_rollback(first, INSTALLED, DEVICE)


def test_unresolved_operation_blocks_new_mutation_but_allows_its_rollback_record(
    tmp_path: Path,
) -> None:
    journal = StateJournal(tmp_path / "state.sqlite3")
    original = journal.begin(
        package="app.two",
        action="disable",
        before=INSTALLED,
        device=DEVICE,
        inverse_action="enable",
    )
    journal.fail(original, "post-state read failed")
    with pytest.raises(ControllerRefusal, match="unfinished or uncertain"):
        journal.begin(
            package="app.two",
            action="enable",
            before=DISABLED,
            device=DEVICE,
            inverse_action="disable",
        )

    rollback = journal.begin(
        package="app.two",
        action="rollback:disable",
        before=DISABLED,
        device=DEVICE,
        parent_id=original,
    )
    assert rollback > original


def test_profile_round_trip_and_completeness(tmp_path: Path) -> None:
    store = StateProfileStore(tmp_path / "profiles")
    profile = store.save(
        "Stable week 1",
        DEVICE,
        [ProfilePackage("app.one", True, 10, "1.0", "app.one-10.apk")],
    )
    assert profile.complete is True
    assert store.get(profile.id) == profile
    incomplete = store.save(
        "Missing archive",
        DEVICE,
        [ProfilePackage("app.two", False, 2, "2.0", "")],
    )
    assert incomplete.complete is False


def test_install_and_selective_rollback_leave_other_package_untouched(tmp_path: Path) -> None:
    StatefulAdb.states = {"app.other": INSTALLED}
    jobs = manager(tmp_path)
    filename = archive(jobs.repository, "app.one", 10)
    installed = wait(jobs, jobs.start_install(filename))
    assert installed["stage"] == "result"
    operation = jobs.journal.list()[0]
    assert operation.action == "install"

    applied(jobs.journal, "app.other", INSTALLED, DISABLED)
    reverted = wait(jobs, jobs.start_rollback(operation.id))
    assert reverted["stage"] == "result"
    assert StatefulAdb.states["app.one"] == ABSENT
    assert StatefulAdb.states["app.other"] == INSTALLED


def test_rolling_back_middle_same_package_restores_previous_rollback_branch(
    tmp_path: Path,
) -> None:
    StatefulAdb.states = {}
    jobs = manager(tmp_path)
    filename = archive(jobs.repository, "app.one", 10)
    assert wait(jobs, jobs.start_install(filename))["stage"] == "result"
    install_operation = jobs.journal.list()[0]
    assert wait(
        jobs, jobs.start_set_enabled("app.one", enabled=False)
    )["stage"] == "result"
    disable_operation = jobs.journal.list()[0]

    assert wait(jobs, jobs.start_rollback(disable_operation.id))["stage"] == "result"
    assert StatefulAdb.states["app.one"].enabled is True
    assert wait(jobs, jobs.start_rollback(install_operation.id))["stage"] == "result"
    assert StatefulAdb.states["app.one"] == ABSENT


def test_profile_capture_preview_and_apply_exact_archived_version(tmp_path: Path) -> None:
    StatefulAdb.states = {"app.one": INSTALLED}
    jobs = manager(tmp_path)
    archive(jobs.repository, "app.one", 10)
    captured = wait(jobs, jobs.start_profile_capture("Stable"))
    assert captured["stage"] == "result"
    profile = jobs.profiles.list()[0]
    assert profile.complete is True

    StatefulAdb.states["app.one"] = ABSENT
    preview = jobs.profile_preview(profile.id)
    assert preview["ready"] is True
    assert preview["operations"] == [{"package": "app.one", "action": "install"}]
    restored = wait(jobs, jobs.start_profile_apply(profile.id))
    assert restored["stage"] == "result"
    assert StatefulAdb.states["app.one"].version_code == 10


def test_apk_required_by_profile_cannot_be_deleted(tmp_path: Path) -> None:
    StatefulAdb.states = {"app.one": INSTALLED}
    jobs = manager(tmp_path)
    filename = archive(jobs.repository, "app.one", 10)
    wait(jobs, jobs.start_profile_capture("Stable"))
    with pytest.raises(ControllerRefusal, match="retained by state profile"):
        jobs.delete_local_apk(filename)
