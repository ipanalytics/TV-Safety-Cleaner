from __future__ import annotations

import subprocess

import pytest

from tv_observer.adb import AdbError, DeviceState, ReadOnlyAdb, parse_devices


class FakeRunner:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, "failure")


def test_allowlist_and_fixed_arguments() -> None:
    fake = FakeRunner("package:com.example.app\n")
    client = ReadOnlyAdb(serial="10.0.0.2:5555", runner=fake)
    assert "packages" in client.allowlist
    assert "com.example.app" in client.packages()
    assert fake.calls == [
        ["adb", "-s", "10.0.0.2:5555", "shell", "pm", "list", "packages"]
    ]


@pytest.mark.parametrize("value", ["x;reboot", "x$(id)", "x && id", "x\nrm"])
def test_injection_serial_rejected_before_runner(value: str) -> None:
    fake = FakeRunner()
    with pytest.raises(AdbError):
        ReadOnlyAdb(serial=value, runner=fake)
    assert fake.calls == []


def test_setting_rejects_bad_namespace_and_key() -> None:
    fake = FakeRunner()
    client = ReadOnlyAdb(runner=fake)
    with pytest.raises(AdbError):
        client.setting("global;settings put", "x")
    with pytest.raises(AdbError):
        client.setting("global", "x;pm clear")
    assert fake.calls == []


def test_device_statuses_and_malformed() -> None:
    devices = parse_devices(
        "List of devices attached\nA device product:x\nB offline\nC unauthorized\nbroken\n"
    )
    assert [item.state for item in devices] == [
        DeviceState.CONNECTED,
        DeviceState.OFFLINE,
        DeviceState.UNAUTHORIZED,
        DeviceState.MALFORMED,
    ]
    assert parse_devices("List of devices attached\n") == []


def test_timeout_and_huge_output() -> None:
    def timeout(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired("adb", 1)

    with pytest.raises(AdbError, match="timed out"):
        ReadOnlyAdb(runner=timeout).properties()
    with pytest.raises(AdbError, match="safety limit"):
        ReadOnlyAdb(runner=FakeRunner("x" * 2_000_001)).properties()
