from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from tv_controller.profile import DeviceProfile, verify_profile
from tv_controller.snapshot import ControllerRefusal, VerifiedSnapshot


class PackageCategory(StrEnum):
    CONFIRMED_SAFE = "confirmed-safe"
    PROBABLY_SAFE = "probably-safe"
    TELEMETRY_CANDIDATE = "telemetry-candidate"
    ADVERTISING_CANDIDATE = "advertising-candidate"
    RISKY = "risky"
    NEVER_TOUCH = "never-touch"
    UNKNOWN = "unknown"


PROTECTED_EXACT = frozenset(
    {
        "android",
        "com.android.systemui",
        "com.android.settings",
        "com.android.packageinstaller",
        "com.android.permissioncontroller",
        "com.android.providers.downloads",
        "com.android.providers.media",
        "com.android.networkstack",
        "com.google.android.tvlauncher",
        "com.amazon.tv.launcher",
        "com.amazon.device.software.ota",
        "com.amazon.settings.systemupdates",
    }
)
PROTECTED_TOKENS = (
    "launcher",
    "systemui",
    "settings",
    "packageinstaller",
    "permissioncontroller",
    "downloadprovider",
    "networkstack",
    "wifi",
    "bluetooth",
    "hdmi",
    "cec",
    "tuner",
    "livetv",
    "audio",
    "storage",
    "setupwizard",
    "recovery",
)


@dataclass(frozen=True)
class PlannedOperation:
    package: str
    category: PackageCategory
    action: str = "future-disable-request"


@dataclass(frozen=True)
class DryRunPlan:
    profile_id: str
    snapshot_fingerprint: str
    operations: tuple[PlannedOperation, ...]
    restore_plan: dict[str, object]
    mutating_calls: int = 0
    mode: str = "dry-run"


def is_never_touch(package: str) -> bool:
    lowered = package.lower()
    return lowered in PROTECTED_EXACT or any(token in lowered for token in PROTECTED_TOKENS)


def _packages(profile: DeviceProfile) -> dict[str, list[str]]:
    raw = profile.packages.get("packages", {})
    if not isinstance(raw, dict):
        raise ControllerRefusal("profile package policy is malformed")
    result: dict[str, list[str]] = {}
    for key, values in raw.items():
        if not isinstance(key, str) or not isinstance(values, list) or not all(
            isinstance(value, str) for value in values
        ):
            raise ControllerRefusal("profile package policy is malformed")
        result[key] = values
    return result


def build_dry_run(
    profile: DeviceProfile,
    snapshot: VerifiedSnapshot,
    *,
    connection_verified: bool,
    authorization_verified: bool,
) -> DryRunPlan:
    verify_profile(profile, snapshot)
    if not connection_verified:
        raise ControllerRefusal("ADB connection is not verified")
    if not authorization_verified:
        raise ControllerRefusal("ADB authorization is not verified")
    policy = _packages(profile)
    all_listed = [package for values in policy.values() for package in values]
    if len(all_listed) != len(set(all_listed)):
        raise ControllerRefusal("package appears in multiple policy categories")
    protected = [package for package in all_listed if is_never_touch(package)]
    if protected:
        raise ControllerRefusal(f"never-touch package in profile: {protected[0]}")
    confirmed = policy.get("confirmed_safe", [])
    operations = tuple(
        PlannedOperation(package, PackageCategory.CONFIRMED_SAFE) for package in sorted(confirmed)
    )
    return DryRunPlan(
        profile.profile_id,
        str(snapshot.manifest["fingerprint"]),
        operations,
        snapshot.restore_plan,
    )
