from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

MAX_OUTPUT_BYTES = 2_000_000
SAFE_VALUE = re.compile(r"^[A-Za-z0-9._:/-]+$")


class AdbError(RuntimeError):
    """Safe, user-facing ADB failure."""


class DeviceState(StrEnum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    UNAUTHORIZED = "unauthorized"
    OFFLINE = "offline"
    TIMEOUT = "timeout"
    MALFORMED = "malformed"


class ReadOperation(StrEnum):
    DEVICES = "devices"
    PROPERTIES = "properties"
    PACKAGES = "packages"
    USER_PACKAGES = "user-packages"
    SYSTEM_PACKAGES = "system-packages"
    DISABLED_PACKAGES = "disabled-packages"
    ACTIVITY_SUMMARY = "activity-summary"
    PACKAGE_SUMMARY = "package-summary"
    MEMORY_SUMMARY = "memory-summary"
    STORAGE_SUMMARY = "storage-summary"
    PROCESS_SUMMARY = "process-summary"
    NETWORK_SUMMARY = "network-summary"
    CPU_SUMMARY = "cpu-summary"
    SETTING_READ = "setting-read"


@dataclass(frozen=True)
class Device:
    serial: str
    state: DeviceState
    details: str = ""


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _safe(value: str, label: str) -> str:
    if not value or not SAFE_VALUE.fullmatch(value):
        raise AdbError(f"invalid {label}")
    return value


def parse_devices(output: str) -> list[Device]:
    devices: list[Device] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        fields = line.split()
        if len(fields) < 2:
            devices.append(Device(fields[0] if fields else "?", DeviceState.MALFORMED))
            continue
        serial, raw_state = fields[:2]
        states = {
            "device": DeviceState.CONNECTED,
            "offline": DeviceState.OFFLINE,
            "unauthorized": DeviceState.UNAUTHORIZED,
        }
        devices.append(
            Device(serial, states.get(raw_state, DeviceState.MALFORMED), " ".join(fields[2:]))
        )
    return devices


class ReadOnlyAdb:
    """ADB adapter whose public surface contains only fixed inventory reads."""

    def __init__(
        self,
        executable: str = "adb",
        serial: str | None = None,
        timeout: float = 15.0,
        runner: Runner = subprocess.run,
    ) -> None:
        self._executable = executable
        self._serial = _safe(serial, "serial") if serial else None
        self._timeout = timeout
        self._runner = runner

    @property
    def allowlist(self) -> tuple[str, ...]:
        return tuple(operation.value for operation in ReadOperation)

    def _read(self, args: list[str]) -> str:
        prefix = [self._executable]
        if self._serial:
            prefix.extend(["-s", self._serial])
        try:
            result = self._runner(
                [*prefix, *args],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AdbError("ADB read timed out") from exc
        if result.returncode != 0:
            raise AdbError(f"ADB read failed: {result.stderr.strip()[:300]}")
        encoded = result.stdout.encode("utf-8", errors="replace")
        if len(encoded) > MAX_OUTPUT_BYTES:
            raise AdbError("ADB output exceeded safety limit")
        return result.stdout

    def devices(self) -> list[Device]:
        return parse_devices(self._read(["devices", "-l"]))

    def properties(self) -> str:
        return self._read(["shell", "getprop"])

    def packages(
        self,
        disabled_only: bool = False,
        *,
        user_only: bool = False,
        system_only: bool = False,
    ) -> str:
        if sum((disabled_only, user_only, system_only)) > 1:
            raise AdbError("select only one package filter")
        args = ["shell", "pm", "list", "packages"]
        if disabled_only:
            args.append("-d")
        elif user_only:
            args.append("-3")
        elif system_only:
            args.append("-s")
        return self._read(args)

    def activity_summary(self) -> str:
        return self._read(["shell", "dumpsys", "activity", "activities"])

    def package_summary(self) -> str:
        return self._read(["shell", "dumpsys", "package"])

    def memory_summary(self) -> str:
        return self._read(["shell", "cat", "/proc/meminfo"])

    def storage_summary(self) -> str:
        return self._read(["shell", "df", "-k"])

    def process_summary(self) -> str:
        return self._read(["shell", "ps", "-A"])

    def network_summary(self) -> str:
        return self._read(["shell", "ip", "addr", "show"])

    def cpu_summary(self) -> str:
        return self._read(["shell", "dumpsys", "cpuinfo"])

    def setting(self, namespace: str, key: str) -> str:
        if namespace not in {"global", "secure", "system"}:
            raise AdbError("invalid settings namespace")
        return self._read(["shell", "settings", "get", namespace, _safe(key, "settings key")])
