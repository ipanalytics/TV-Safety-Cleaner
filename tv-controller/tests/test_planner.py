from __future__ import annotations

from pathlib import Path

import pytest
from test_controller_snapshot import make_profile, make_snapshot

from tv_controller.planner import PROTECTED_EXACT, build_dry_run, is_never_touch
from tv_controller.profile import load_profile
from tv_controller.snapshot import ControllerRefusal, load_snapshot


def add_packages(profile: Path, confirmed: list[str], probably: list[str] | None = None) -> None:
    probably = probably or []
    values = ", ".join(f'"{item}"' for item in confirmed)
    lower = ", ".join(f'"{item}"' for item in probably)
    (profile / "packages.toml").write_text(
        f"[packages]\nconfirmed_safe = [{values}]\nprobably_safe = [{lower}]\n",
        encoding="utf-8",
    )


def test_dry_run_only_uses_confirmed_and_displays_restore(tmp_path: Path) -> None:
    profile_path = make_profile(tmp_path)
    add_packages(profile_path, ["com.vendor.optional"], ["com.vendor.uncertain"])
    plan = build_dry_run(
        load_profile(profile_path),
        load_snapshot(make_snapshot(tmp_path)),
        connection_verified=True,
        authorization_verified=True,
    )
    assert [item.package for item in plan.operations] == ["com.vendor.optional"]
    assert plan.restore_plan["automatic_restore"] is False
    assert plan.mutating_calls == 0
    assert plan.mode == "dry-run"


@pytest.mark.parametrize(("connection", "authorization"), [(False, True), (True, False)])
def test_connection_authorization_gates(
    tmp_path: Path, connection: bool, authorization: bool
) -> None:
    with pytest.raises(ControllerRefusal, match="not verified"):
        build_dry_run(
            load_profile(make_profile(tmp_path)),
            load_snapshot(make_snapshot(tmp_path)),
            connection_verified=connection,
            authorization_verified=authorization,
        )


@pytest.mark.parametrize(
    "package",
    [
        "com.android.systemui",
        "com.android.settings",
        "com.vendor.launcher",
        "com.vendor.wifi.service",
        "com.vendor.hdmi.cec",
        "com.vendor.livetv.tuner",
        "com.vendor.audio",
        "com.vendor.recovery",
    ],
)
def test_never_touch_overrides_profile(tmp_path: Path, package: str) -> None:
    profile_path = make_profile(tmp_path)
    add_packages(profile_path, [package])
    with pytest.raises(ControllerRefusal, match="never-touch"):
        build_dry_run(
            load_profile(profile_path),
            load_snapshot(make_snapshot(tmp_path)),
            connection_verified=True,
            authorization_verified=True,
        )


def test_protected_policy_has_core_components() -> None:
    assert "com.android.systemui" in PROTECTED_EXACT
    assert "com.amazon.device.software.ota" in PROTECTED_EXACT
    assert is_never_touch("com.vendor.bluetooth.service")


def test_unknown_tcl_template_refuses(tmp_path: Path) -> None:
    template = Path("tv-controller/profiles/tcl/65t6c/FIRE_OS_BUILD")
    snapshot = load_snapshot(
        make_snapshot(
            tmp_path,
            firmware="REPLACE_WITH_EXACT_BUILD",
            fingerprint="REPLACE_WITH_EXACT_FINGERPRINT",
        )
    )
    with pytest.raises(ControllerRefusal, match="not verified"):
        build_dry_run(
            load_profile(template),
            snapshot,
            connection_verified=True,
            authorization_verified=True,
        )
