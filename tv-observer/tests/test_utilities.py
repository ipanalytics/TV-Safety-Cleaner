from __future__ import annotations

import json
import zipfile
from dataclasses import replace
from pathlib import Path

from tv_observer.snapshot import SnapshotPayload, create_snapshot, verify_snapshot
from tv_observer.utilities import archive_snapshot, diff_snapshots, format_diff, redact_snapshot


def payload(**changes: object) -> SnapshotPayload:
    base = SnapshotPayload(
        model="TCL 65T6C",
        platform="amazon-fire-os",
        firmware="1",
        fingerprint="fp1",
        security_patch="2026-01-01",
        serial="SECRET-SERIAL",
        packages=[{"name": "app.old", "version": "1"}, {"name": "app.same", "version": "1"}],
        launcher="launcher.old",
        settings={"account_email": "person@example.com", "endpoint": "https://x.test/a?token=abc"},
        processes=["old"],
    )
    return replace(base, **changes)


def test_diff_text_and_json_data(tmp_path: Path) -> None:
    before = create_snapshot(tmp_path, "before", payload())
    after = create_snapshot(
        tmp_path,
        "after",
        payload(
            firmware="2",
            fingerprint="fp2",
            security_patch="2026-02-01",
            platform="android-tv",
            packages=[{"name": "app.new", "version": "1"}, {"name": "app.same", "version": "2"}],
            disabled_packages=["app.new"],
            launcher="launcher.new",
            settings={"mode": "new"},
            processes=["new"],
        ),
    )
    value = diff_snapshots(before, after)
    assert value["packages_added"] == ["app.new"]
    assert value["packages_removed"] == ["app.old"]
    assert "app.same" in value["packages_changed"]
    assert set(value["firmware"]) == {"version", "fingerprint", "security_patch", "platform"}
    assert "launcher" in format_diff(value)


def test_archive_is_safe_and_valid(tmp_path: Path) -> None:
    source = create_snapshot(tmp_path, "tv", payload())
    archive = archive_snapshot(source)
    assert archive.is_file()
    with zipfile.ZipFile(archive) as opened:
        assert opened.testzip() is None
        assert all(".." not in Path(name).parts for name in opened.namelist())


def test_redact_copy_leaves_source_unchanged(tmp_path: Path) -> None:
    source = create_snapshot(tmp_path, "tv", payload())
    original = (source / "device.json").read_bytes()
    redacted = redact_snapshot(source)
    assert (source / "device.json").read_bytes() == original
    device = json.loads((redacted / "device.json").read_text(encoding="utf-8"))
    settings = json.loads((redacted / "settings.json").read_text(encoding="utf-8"))
    assert device["serial"] == "[REDACTED]"
    assert settings["account_email"] == "[REDACTED]"
    assert "token=abc" not in json.dumps(settings)
    verify_snapshot(redacted)
