from __future__ import annotations

import json
from pathlib import Path

import pytest

from tv_observer.snapshot import (
    JSON_FILES,
    SnapshotError,
    SnapshotPayload,
    create_snapshot,
    inspect_snapshot,
    list_snapshots,
    verify_snapshot,
)


@pytest.fixture
def payload() -> SnapshotPayload:
    return SnapshotPayload(
        model="TCL 65T6C",
        platform="amazon-fire-os",
        firmware="PS7613/3216",
        fingerprint="TCL/65T6C/firetv:11/build",
        security_patch="2026-05-01",
        serial="SERIAL-TEST",
        packages=[{"name": "com.example.app", "version": "1"}],
        launcher="com.amazon.tv.launcher",
    )


def test_atomic_snapshot_and_json(tmp_path: Path, payload: SnapshotPayload) -> None:
    result = create_snapshot(tmp_path, "living-room", payload)
    assert result in list_snapshots(tmp_path)
    assert set(JSON_FILES).issubset({path.name for path in result.iterdir()})
    assert (result / "recovery-plan.md").is_file()
    assert (result / "checksums.sha256").is_file()
    for name in JSON_FILES:
        json.loads((result / name).read_text(encoding="utf-8"))
    assert inspect_snapshot(result)["kind"] == "inventory-snapshot"


def test_partial_and_invalid_snapshots_hidden(tmp_path: Path, payload: SnapshotPayload) -> None:
    device = tmp_path / "living-room"
    (device / ".partial-crash").mkdir(parents=True)
    (device / "invalid").mkdir()
    ready = create_snapshot(tmp_path, "living-room", payload)
    assert list_snapshots(tmp_path) == [ready]


def test_checksum_tampering_fails(tmp_path: Path, payload: SnapshotPayload) -> None:
    result = create_snapshot(tmp_path, "living-room", payload)
    (result / "device.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(SnapshotError, match="checksum mismatch"):
        verify_snapshot(result)


def test_unsafe_device_name_rejected(tmp_path: Path, payload: SnapshotPayload) -> None:
    with pytest.raises(SnapshotError, match="unsafe"):
        create_snapshot(tmp_path, "../escape", payload)


def test_unchecked_or_symlink_entry_rejected(tmp_path: Path, payload: SnapshotPayload) -> None:
    result = create_snapshot(tmp_path, "living-room", payload)
    (result / "unchecked.txt").write_text("untrusted", encoding="utf-8")
    with pytest.raises(SnapshotError, match="unsupported or unchecked"):
        verify_snapshot(result)
