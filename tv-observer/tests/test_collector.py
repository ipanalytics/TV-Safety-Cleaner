from __future__ import annotations

import subprocess

import pytest

from tv_observer.adb import AdbError
from tv_observer.collector import collect_snapshot_payload


class FixtureRunner:
    def __init__(self, state: str = "device") -> None:
        self.state = state
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        tail = args[args.index("shell") :] if "shell" in args else args[1:]
        values = {
            ("devices", "-l"): f"List of devices attached\nTV123 {self.state} model:TCL\n",
            ("shell", "getprop"): (
                "[ro.product.model]: [TCL 65T6C]\n"
                "[ro.product.manufacturer]: [Amazon]\n"
                "[ro.build.product]: [firetv]\n"
                "[ro.build.version.incremental]: [build-1]\n"
                "[ro.build.fingerprint]: [tcl/fire/build-1]\n"
                "[ro.build.version.security_patch]: [2026-01-01]\n"
            ),
            ("shell", "pm", "list", "packages"): (
                "package:com.amazon.device.messaging\npackage:com.example.app\n"
            ),
            ("shell", "pm", "list", "packages", "-d"): "package:com.example.disabled\n",
            ("shell", "pm", "list", "packages", "-3"): "package:com.example.app\n",
            ("shell", "pm", "list", "packages", "-s"): (
                "package:com.amazon.device.messaging\n"
            ),
            ("shell", "dumpsys", "activity", "activities"): (
                "mResumedActivity: ActivityRecord x com.amazon.tv.launcher/.Main"
            ),
            ("shell", "dumpsys", "package"): (
                "Packages:\n"
                "  Package [com.amazon.device.messaging] (abc):\n"
                "    codePath=/system/priv-app/Messaging\n"
                "    versionName=1.2\n"
                "    requested permissions:\n"
                "      android.permission.INTERNET\n"
                "  Package [com.example.app] (def):\n"
                "    codePath=/data/app/example\n"
                "    versionName=3.4\n"
                "    requested permissions:\n"
                "      android.permission.ACCESS_NETWORK_STATE\n"
            ),
            ("shell", "ps", "-A"): "PID NAME\n1 init\n",
            ("shell", "cat", "/proc/meminfo"): "MemTotal: 1024 kB",
            ("shell", "df", "-k"): "/dev/block 100 50 50",
            ("shell", "ip", "addr", "show"): "1: lo: <LOOPBACK>",
            ("shell", "settings", "get", "global", "adb_enabled"): "1\n",
            ("shell", "settings", "get", "global", "stay_on_while_plugged_in"): "0\n",
            ("shell", "settings", "get", "system", "screen_off_timeout"): "600000\n",
        }
        key = tuple(tail)
        return subprocess.CompletedProcess(args, 0, values.get(key, ""), "")


def test_collects_realistic_payload_with_only_read_calls() -> None:
    runner = FixtureRunner()
    payload = collect_snapshot_payload(runner=runner)
    assert payload.model == "TCL 65T6C"
    assert payload.platform == "amazon-fire-os"
    assert payload.firmware == "build-1"
    assert payload.launcher == "com.amazon.tv.launcher"
    assert payload.disabled_packages == ["com.example.disabled"]
    assert payload.packages == [
        {
            "name": "com.amazon.device.messaging",
            "version": "1.2",
            "install_level": "system",
            "scope": "current-user",
            "enabled": True,
            "requested_permissions": ["android.permission.INTERNET"],
        },
        {
            "name": "com.example.app",
            "version": "3.4",
            "install_level": "user",
            "scope": "current-user",
            "enabled": True,
            "requested_permissions": ["android.permission.ACCESS_NETWORK_STATE"],
        },
    ]
    assert payload.network_summary == "1: lo: <LOOPBACK>"
    assert all(isinstance(call, list) for call in runner.calls)
    joined = [" ".join(call) for call in runner.calls]
    assert not any(" disable " in f" {call} " or " uninstall " in f" {call} " for call in joined)


@pytest.mark.parametrize("state", ["offline", "unauthorized"])
def test_collection_refuses_unready_device(state: str) -> None:
    with pytest.raises(AdbError, match="select exactly one connected"):
        collect_snapshot_payload(runner=FixtureRunner(state))
