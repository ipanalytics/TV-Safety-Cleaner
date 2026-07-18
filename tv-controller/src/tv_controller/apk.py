from __future__ import annotations

import hashlib
import json
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tv_controller.planner import is_never_touch
from tv_controller.snapshot import ControllerRefusal

MAX_APK_BYTES = 500 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 20_000
SAFE_PACKAGE = re.compile(r"^[A-Za-z0-9._]+$")
FORBIDDEN_ARCHIVE_ROOTS = (
    Path("/srv/media"),
    Path("/srv/media-disk"),
    Path("/srv/downloads"),
    Path("/srv/pi-media-stack"),
)


class UserApkAdapter(Protocol):
    def install_user(self, apk: Path, replace: bool) -> None: ...

    def uninstall_user(self, package: str) -> None: ...


@dataclass(frozen=True)
class ApkMetadata:
    package_name: str
    version: str
    signature_summary: str
    abi: tuple[str, ...]
    minimum_sdk: str
    size: int


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def inspect_apk(path: Path) -> ApkMetadata:
    try:
        apk = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ControllerRefusal("APK does not exist") from exc
    if not apk.is_file() or path.is_symlink() or apk.suffix.lower() != ".apk":
        raise ControllerRefusal("APK must be a real .apk file")
    size = apk.stat().st_size
    if size > MAX_APK_BYTES:
        raise ControllerRefusal("APK exceeds configured size limit")
    try:
        with zipfile.ZipFile(apk) as archive:
            names = archive.namelist()
            if len(names) > MAX_ARCHIVE_ENTRIES:
                raise ControllerRefusal("APK contains too many entries")
            if sum(item.file_size for item in archive.infolist()) > MAX_APK_BYTES:
                raise ControllerRefusal("APK expanded content exceeds safety limit")
            if any(Path(name).is_absolute() or ".." in Path(name).parts for name in names):
                raise ControllerRefusal("APK contains unsafe paths")
            metadata: dict[str, object] = {}
            if "tv-safety-metadata.json" in names:
                raw = archive.read("tv-safety-metadata.json")
                metadata = json.loads(raw.decode("utf-8"))
            abi = sorted(
                {
                    parts[1]
                    for name in names
                    if len(parts := Path(name).parts) >= 3 and parts[0] == "lib"
                }
            )
            signatures = sorted(
                name
                for name in names
                if name.upper().startswith("META-INF/")
                and name.upper().endswith((".RSA", ".DSA", ".EC"))
            )
            signature = (
                ",".join(f"{name}:{_sha256(archive.read(name))[:16]}" for name in signatures)
                if signatures
                else "unavailable"
            )
    except (zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControllerRefusal("APK archive or metadata is invalid") from exc
    return ApkMetadata(
        package_name=str(metadata.get("package_name", "unavailable")),
        version=str(metadata.get("version", "unavailable")),
        signature_summary=signature,
        abi=tuple(abi),
        minimum_sdk=str(metadata.get("minimum_sdk", "unavailable")),
        size=size,
    )


def archive_apk(path: Path, archive_root: Path) -> Path:
    metadata = inspect_apk(path)
    root = archive_root.resolve()
    if root in FORBIDDEN_ARCHIVE_ROOTS or any(
        base in root.parents for base in FORBIDDEN_ARCHIVE_ROOTS
    ):
        raise ControllerRefusal("APK archive cannot use media or download storage")
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    package = metadata.package_name.replace("/", "_").replace("..", "_")
    destination = root / f"{package}-{metadata.version}-{digest}.apk"
    if not destination.exists():
        shutil.copy2(path, destination)
    return destination


class UserApkManager:
    def __init__(
        self,
        adapter: UserApkAdapter,
        feature_enabled: bool = False,
        user_packages: frozenset[str] = frozenset(),
        system_packages: frozenset[str] = frozenset(),
    ) -> None:
        self.adapter = adapter
        self.feature_enabled = feature_enabled
        self.user_packages = user_packages
        self.system_packages = system_packages

    def _gate(self, confirmed: bool, system_app: bool = False) -> None:
        if system_app:
            raise ControllerRefusal("system APK removal is prohibited")
        if not self.feature_enabled:
            raise ControllerRefusal("user APK changes feature is disabled")
        if not confirmed:
            raise ControllerRefusal("explicit confirmation is required")

    def install(self, apk: Path, *, confirmed: bool) -> None:
        self._gate(confirmed)
        metadata = inspect_apk(apk)
        if metadata.package_name in self.system_packages or is_never_touch(metadata.package_name):
            raise ControllerRefusal("APK targets a system or never-touch package")
        self.adapter.install_user(apk, replace=False)

    def update(self, apk: Path, *, confirmed: bool) -> None:
        self._gate(confirmed)
        metadata = inspect_apk(apk)
        if metadata.package_name not in self.user_packages:
            raise ControllerRefusal("APK update requires a verified user package")
        self.adapter.install_user(apk, replace=True)

    def uninstall(self, package: str, *, confirmed: bool, system_app: bool) -> None:
        self._gate(confirmed, system_app)
        if not SAFE_PACKAGE.fullmatch(package):
            raise ControllerRefusal("invalid user package name")
        if package not in self.user_packages or is_never_touch(package):
            raise ControllerRefusal("system or unknown APK removal is prohibited")
        self.adapter.uninstall_user(package)
