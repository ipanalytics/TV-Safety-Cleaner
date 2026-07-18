from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tv_controller.snapshot import SUPPORTED_PLATFORMS, ControllerRefusal, VerifiedSnapshot


@dataclass(frozen=True)
class DeviceProfile:
    path: Path
    profile_id: str
    platform: str
    model: str
    firmware: str
    fingerprint: str
    verified: bool
    recovery_status: str
    packages: dict[str, Any]
    checks: dict[str, Any]


def _toml(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise ControllerRefusal(f"profile file missing: {path.name}")
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ControllerRefusal(f"profile file invalid: {path.name}") from exc


def load_profile(path: Path) -> DeviceProfile:
    try:
        directory = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ControllerRefusal("profile does not exist") from exc
    if not directory.is_dir() or path.is_symlink():
        raise ControllerRefusal("profile must be a real directory")
    metadata = _toml(directory / "profile.toml").get("profile", {})
    packages = _toml(directory / "packages.toml")
    checks = _toml(directory / "checks.toml")
    required = ("id", "platform", "model", "firmware", "fingerprint", "recovery_status")
    missing = [name for name in required if name not in metadata]
    if missing:
        raise ControllerRefusal(f"profile metadata missing: {', '.join(missing)}")
    return DeviceProfile(
        path=directory,
        profile_id=str(metadata["id"]),
        platform=str(metadata["platform"]),
        model=str(metadata["model"]),
        firmware=str(metadata["firmware"]),
        fingerprint=str(metadata["fingerprint"]),
        verified=metadata.get("verified") is True,
        recovery_status=str(metadata["recovery_status"]),
        packages=packages,
        checks=checks,
    )


def verify_profile(profile: DeviceProfile, snapshot: VerifiedSnapshot) -> None:
    manifest = snapshot.manifest
    if manifest.get("platform") not in SUPPORTED_PLATFORMS:
        raise ControllerRefusal("unsupported or unknown snapshot platform")
    if profile.platform != manifest.get("platform"):
        raise ControllerRefusal("profile platform mismatch")
    if profile.model != manifest.get("model"):
        raise ControllerRefusal("profile model mismatch")
    if profile.firmware != manifest.get("firmware"):
        raise ControllerRefusal("profile firmware mismatch")
    if profile.fingerprint != manifest.get("fingerprint"):
        raise ControllerRefusal("profile fingerprint mismatch")
    if not profile.verified:
        raise ControllerRefusal("profile is not verified")
    if profile.recovery_status != "Verified on this device":
        raise ControllerRefusal("recovery readiness is insufficient")
