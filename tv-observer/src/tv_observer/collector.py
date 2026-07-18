from __future__ import annotations

import re

from tv_observer.adb import AdbError, DeviceState, ReadOnlyAdb, Runner
from tv_observer.platform import PlatformEvidence, detect_platform, parse_properties
from tv_observer.snapshot import SnapshotPayload

PACKAGE_LINE = re.compile(r"^package:(?P<name>[A-Za-z0-9._]+)$")
PACKAGE_HEADER = re.compile(r"^\s*Package \[(?P<name>[A-Za-z0-9._]+)\]")
PERMISSION_LINE = re.compile(r"^(?P<name>[A-Za-z0-9._]+)(?::.*)?$")
FOREGROUND = re.compile(r"(?:mResumedActivity|topResumedActivity).*? ([A-Za-z0-9._]+)/")


def _package_names(raw: str) -> list[str]:
    names: list[str] = []
    for line in raw.splitlines():
        match = PACKAGE_LINE.fullmatch(line.strip())
        if match:
            names.append(match.group("name"))
    return sorted(set(names))


def _package_details(raw: str) -> dict[str, dict[str, object]]:
    details: dict[str, dict[str, object]] = {}
    current: dict[str, object] | None = None
    in_requested_permissions = False
    for line in raw.splitlines():
        header = PACKAGE_HEADER.match(line)
        if header:
            current = details.setdefault(
                header.group("name"),
                {"version": "unknown", "code_path": "unknown", "requested_permissions": []},
            )
            in_requested_permissions = False
            continue
        if current is None:
            continue
        stripped = line.strip()
        if stripped.startswith("versionName="):
            current["version"] = stripped.removeprefix("versionName=") or "unknown"
            in_requested_permissions = False
        elif stripped.startswith("codePath="):
            current["code_path"] = stripped.removeprefix("codePath=") or "unknown"
            in_requested_permissions = False
        elif stripped == "requested permissions:":
            in_requested_permissions = True
        elif stripped.endswith("permissions:") or not stripped:
            in_requested_permissions = False
        elif in_requested_permissions:
            permission = PERMISSION_LINE.fullmatch(stripped)
            permissions = current["requested_permissions"]
            if permission and isinstance(permissions, list):
                permissions.append(permission.group("name"))
    return details


def _package_inventory(
    package_names: list[str],
    user_packages: set[str],
    system_packages: set[str],
    disabled_packages: set[str],
    summary: str,
) -> list[dict[str, object]]:
    details = _package_details(summary)
    inventory: list[dict[str, object]] = []
    for name in package_names:
        package = details.get(name, {})
        code_path = str(package.get("code_path", "unknown"))
        if name in user_packages:
            level = "user"
        elif name in system_packages and code_path.startswith("/data/"):
            level = "updated-system"
        elif name in system_packages:
            level = "system"
        else:
            level = "unknown"
        permissions = package.get("requested_permissions", [])
        inventory.append(
            {
                "name": name,
                "version": str(package.get("version", "unknown")),
                "install_level": level,
                "scope": "current-user",
                "enabled": name not in disabled_packages,
                "requested_permissions": permissions if isinstance(permissions, list) else [],
            }
        )
    return inventory


def collect_snapshot_payload(
    serial: str | None = None,
    *,
    executable: str = "adb",
    runner: Runner | None = None,
) -> SnapshotPayload:
    """Collect a bounded read-only inventory from exactly one authorized ADB device."""

    discovery = (
        ReadOnlyAdb(executable=executable)
        if runner is None
        else ReadOnlyAdb(executable=executable, runner=runner)
    )
    devices = discovery.devices()
    if serial is None:
        connected = [device for device in devices if device.state is DeviceState.CONNECTED]
        if len(connected) != 1:
            states = ", ".join(f"{device.serial}:{device.state.value}" for device in devices)
            raise AdbError(f"select exactly one connected device; found {states or 'none'}")
        serial = connected[0].serial
    else:
        matching = [device for device in devices if device.serial == serial]
        if not matching or matching[0].state is not DeviceState.CONNECTED:
            state = matching[0].state.value if matching else "disconnected"
            raise AdbError(f"selected device is not connected: {state}")

    client = (
        ReadOnlyAdb(executable=executable, serial=serial)
        if runner is None
        else ReadOnlyAdb(executable=executable, serial=serial, runner=runner)
    )
    properties_raw = client.properties()
    properties = parse_properties(properties_raw)
    package_names = _package_names(client.packages())
    disabled = set(_package_names(client.packages(disabled_only=True)))
    user_packages = set(_package_names(client.packages(user_only=True)))
    system_packages = set(_package_names(client.packages(system_only=True)))
    package_summary = client.package_summary()
    activity = client.activity_summary()
    foreground = FOREGROUND.search(activity)
    launcher = foreground.group(1) if foreground else "unknown"
    detection = detect_platform(
        PlatformEvidence(properties, frozenset(package_names), launcher=launcher)
    )
    settings = {
        key: client.setting(namespace, key).strip()
        for namespace, key in (
            ("global", "adb_enabled"),
            ("global", "stay_on_while_plugged_in"),
            ("system", "screen_off_timeout"),
        )
    }
    return SnapshotPayload(
        model=properties.get("ro.product.model", "unknown"),
        platform=detection.platform.value,
        firmware=properties.get("ro.build.version.incremental", "unknown"),
        fingerprint=properties.get("ro.build.fingerprint", "unknown"),
        security_patch=properties.get("ro.build.version.security_patch", "unknown"),
        serial=serial,
        packages=_package_inventory(
            package_names, user_packages, system_packages, disabled, package_summary
        ),
        disabled_packages=sorted(disabled),
        launcher=launcher,
        settings=settings,
        processes=client.process_summary().splitlines(),
        package_summary=package_summary,
        memory_summary=client.memory_summary(),
        storage_summary=client.storage_summary(),
        network_summary=client.network_summary(),
    )
