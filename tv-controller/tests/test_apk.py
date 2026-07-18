from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from tv_controller.apk import UserApkManager, archive_apk, inspect_apk
from tv_controller.snapshot import ControllerRefusal


def make_apk(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "tv-safety-metadata.json",
            json.dumps(
                {
                    "package_name": "com.example.userapp",
                    "version": "2.1",
                    "minimum_sdk": "26",
                }
            ),
        )
        archive.writestr("lib/arm64-v8a/libexample.so", b"binary")
        archive.writestr("META-INF/RELEASE.RSA", b"test-signature")
        archive.writestr("AndroidManifest.xml", b"fixture")
    return path


class FakeApkAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def install_user(self, apk: Path, replace: bool) -> None:
        self.calls.append(("install", replace))

    def uninstall_user(self, package: str) -> None:
        self.calls.append(("uninstall", package))


def test_inspect_and_archive(tmp_path: Path) -> None:
    apk = make_apk(tmp_path / "app.apk")
    metadata = inspect_apk(apk)
    assert metadata.package_name == "com.example.userapp"
    assert metadata.version == "2.1"
    assert metadata.minimum_sdk == "26"
    assert metadata.abi == ("arm64-v8a",)
    assert "RELEASE.RSA" in metadata.signature_summary
    archived = archive_apk(apk, tmp_path / "private-apk-archive")
    assert archived.is_file()


def test_feature_and_confirmation_gates(tmp_path: Path) -> None:
    apk = make_apk(tmp_path / "app.apk")
    adapter = FakeApkAdapter()
    with pytest.raises(ControllerRefusal, match="feature is disabled"):
        UserApkManager(adapter).install(apk, confirmed=True)
    manager = UserApkManager(
        adapter,
        feature_enabled=True,
        user_packages=frozenset({"com.example.userapp"}),
    )
    with pytest.raises(ControllerRefusal, match="confirmation"):
        manager.install(apk, confirmed=False)
    assert adapter.calls == []
    manager.install(apk, confirmed=True)
    manager.update(apk, confirmed=True)
    assert adapter.calls == [("install", False), ("install", True)]


def test_system_uninstall_always_refused() -> None:
    adapter = FakeApkAdapter()
    manager = UserApkManager(adapter, feature_enabled=True)
    with pytest.raises(ControllerRefusal, match="system APK"):
        manager.uninstall("com.android.systemui", confirmed=True, system_app=True)
    with pytest.raises(ControllerRefusal, match="system or unknown"):
        manager.uninstall("com.android.systemui", confirmed=True, system_app=False)
    assert adapter.calls == []


def test_user_uninstall_requires_gates() -> None:
    adapter = FakeApkAdapter()
    manager = UserApkManager(
        adapter,
        feature_enabled=True,
        user_packages=frozenset({"com.example.userapp"}),
    )
    with pytest.raises(ControllerRefusal, match="confirmation"):
        manager.uninstall("com.example.userapp", confirmed=False, system_app=False)
    manager.uninstall("com.example.userapp", confirmed=True, system_app=False)
    assert adapter.calls == [("uninstall", "com.example.userapp")]


def test_archive_rejects_media_paths(tmp_path: Path) -> None:
    apk = make_apk(tmp_path / "app.apk")
    with pytest.raises(ControllerRefusal, match="media or download"):
        archive_apk(apk, Path("/srv/downloads/apks"))


def test_unsafe_zip_member_refuses(tmp_path: Path) -> None:
    apk = tmp_path / "unsafe.apk"
    with zipfile.ZipFile(apk, "w") as archive:
        archive.writestr("../escape", b"x")
    with pytest.raises(ControllerRefusal, match="unsafe paths"):
        inspect_apk(apk)
