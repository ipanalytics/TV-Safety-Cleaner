from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tv_controller.profile import load_profile, verify_profile
from tv_controller.snapshot import ControllerRefusal, load_snapshot


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_snapshot(tmp_path: Path, **changes: object) -> Path:
    directory = tmp_path / "snapshot"
    directory.mkdir()
    manifest: dict[str, object] = {
        "schema_version": "1.0",
        "kind": "inventory-snapshot",
        "platform": "amazon-fire-os",
        "model": "TCL 65T6C",
        "firmware": "build-1",
        "fingerprint": "tcl/fire/build-1",
        "security_patch": "2026-01-01",
    }
    manifest.update(changes)
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "restore-plan.json").write_text(
        json.dumps({"automatic_restore": False, "operations": []}), encoding="utf-8"
    )
    lines = [
        f"{digest(directory / name)}  {name}"
        for name in ("manifest.json", "restore-plan.json")
    ]
    (directory / "checksums.sha256").write_text("\n".join(lines) + "\n", encoding="ascii")
    return directory


def make_profile(tmp_path: Path, **changes: object) -> Path:
    directory = tmp_path / "profile"
    directory.mkdir()
    values: dict[str, object] = {
        "id": "test-profile",
        "platform": "amazon-fire-os",
        "model": "TCL 65T6C",
        "firmware": "build-1",
        "fingerprint": "tcl/fire/build-1",
        "verified": True,
        "recovery_status": "Verified on this device",
    }
    values.update(changes)
    lines = ["[profile]"]
    for key, value in values.items():
        encoded = "true" if value is True else "false" if value is False else json.dumps(value)
        lines.append(f"{key} = {encoded}")
    (directory / "profile.toml").write_text("\n".join(lines), encoding="utf-8")
    (directory / "packages.toml").write_text("[packages]\nconfirmed_safe = []\n", encoding="utf-8")
    (directory / "checks.toml").write_text("[checks]\nadb = true\n", encoding="utf-8")
    return directory


def test_load_and_verify_exact_profile(tmp_path: Path) -> None:
    verify_profile(load_profile(make_profile(tmp_path)), load_snapshot(make_snapshot(tmp_path)))


def test_missing_and_invalid_checksum_refuse(tmp_path: Path) -> None:
    with pytest.raises(ControllerRefusal, match="does not exist"):
        load_snapshot(tmp_path / "missing")
    snapshot = make_snapshot(tmp_path)
    (snapshot / "manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ControllerRefusal, match="checksum invalid"):
        load_snapshot(snapshot)


def test_unchecked_snapshot_entry_refuses(tmp_path: Path) -> None:
    snapshot = make_snapshot(tmp_path)
    (snapshot / "extra.txt").write_text("unchecked", encoding="utf-8")
    with pytest.raises(ControllerRefusal, match="unsupported or unchecked"):
        load_snapshot(snapshot)


@pytest.mark.parametrize(
    ("profile_change", "snapshot_change", "message"),
    [
        ({}, {"platform": "unknown"}, "unsupported or unknown"),
        ({"model": "Other"}, {}, "model mismatch"),
        ({"firmware": "other"}, {}, "firmware mismatch"),
        ({"fingerprint": "other"}, {}, "fingerprint mismatch"),
        ({"verified": False}, {}, "not verified"),
        ({"recovery_status": "Documented only"}, {}, "recovery readiness"),
    ],
)
def test_compatibility_refusals(
    tmp_path: Path,
    profile_change: dict[str, object],
    snapshot_change: dict[str, object],
    message: str,
) -> None:
    profile = load_profile(make_profile(tmp_path, **profile_change))
    snapshot = load_snapshot(make_snapshot(tmp_path, **snapshot_change))
    with pytest.raises(ControllerRefusal, match=message):
        verify_profile(profile, snapshot)
