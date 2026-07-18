from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tv_observer import __version__

SCHEMA_VERSION = "1.0"
JSON_FILES = (
    "manifest.json",
    "device.json",
    "firmware.json",
    "packages.json",
    "disabled-packages.json",
    "permissions.json",
    "components.json",
    "launcher.json",
    "settings.json",
    "processes.json",
    "memory.json",
    "storage.json",
    "network-summary.json",
    "restore-plan.json",
    "privacy-report.json",
)
SAFE_DEVICE = re.compile(r"^[A-Za-z0-9._-]+$")


class SnapshotError(RuntimeError):
    """Snapshot is invalid, incomplete, or unsafe to process."""


@dataclass(frozen=True)
class SnapshotPayload:
    model: str
    platform: str
    firmware: str
    fingerprint: str
    security_patch: str
    serial: str = "unknown"
    packages: list[dict[str, Any]] = field(default_factory=list)
    disabled_packages: list[str] = field(default_factory=list)
    launcher: str = "unknown"
    settings: dict[str, str] = field(default_factory=dict)
    processes: list[str] = field(default_factory=list)
    package_summary: str = ""
    memory_summary: str = ""
    storage_summary: str = ""
    network_summary: str = ""


def _json_write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    json.loads(path.read_text(encoding="utf-8"))


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def _write_checksums(directory: Path) -> None:
    files = sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.name != "checksums.sha256"
    )
    lines = [f"{_digest(path)}  {path.name}" for path in files]
    (directory / "checksums.sha256").write_text("\n".join(lines) + "\n", encoding="ascii")


def _safe_snapshot_dir(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_dir() or path.is_symlink():
        raise SnapshotError("snapshot path must be a real directory")
    return resolved


def verify_snapshot(path: Path) -> dict[str, Any]:
    directory = _safe_snapshot_dir(path)
    checksum_file = directory / "checksums.sha256"
    if not checksum_file.is_file() or checksum_file.is_symlink():
        raise SnapshotError("missing checksums.sha256")
    expected_files: set[str] = set()
    for line in checksum_file.read_text(encoding="ascii").splitlines():
        try:
            digest, name = line.split("  ", 1)
        except ValueError as exc:
            raise SnapshotError("malformed checksum line") from exc
        if Path(name).name != name or name in expected_files:
            raise SnapshotError("unsafe or duplicate checksum path")
        target = directory / name
        if not target.is_file() or target.is_symlink() or _digest(target) != digest:
            raise SnapshotError(f"checksum mismatch: {name}")
        expected_files.add(name)
    missing = set(JSON_FILES) - expected_files
    if missing:
        raise SnapshotError(f"missing required artifacts: {', '.join(sorted(missing))}")
    allowed = expected_files | {"checksums.sha256"}
    for entry in directory.iterdir():
        if entry.name not in allowed or not entry.is_file() or entry.is_symlink():
            raise SnapshotError(f"unsupported or unchecked snapshot entry: {entry.name}")
    for name in JSON_FILES:
        json.loads((directory / name).read_text(encoding="utf-8"))
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise SnapshotError("snapshot manifest must be a JSON object")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise SnapshotError("unsupported snapshot schema")
    return manifest


def create_snapshot(root: Path, device_name: str, payload: SnapshotPayload) -> Path:
    if not SAFE_DEVICE.fullmatch(device_name):
        raise SnapshotError("unsafe device name")
    now = datetime.now().astimezone()
    timestamp = now.strftime("%Y%m%dT%H%M%S%z")
    device_dir = root / device_name
    device_dir.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".partial-", dir=device_dir))
    final = device_dir / timestamp
    try:
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "observer_version": __version__,
            "created_utc": now.astimezone(UTC).isoformat(),
            "created_local": now.isoformat(),
            "model": payload.model,
            "platform": payload.platform,
            "firmware": payload.firmware,
            "fingerprint": payload.fingerprint,
            "security_patch": payload.security_patch,
            "package_count": len(payload.packages),
            "kind": "inventory-snapshot",
        }
        artifacts: dict[str, object] = {
            "manifest.json": manifest,
            "device.json": {"model": payload.model, "serial": payload.serial},
            "firmware.json": {
                "version": payload.firmware,
                "fingerprint": payload.fingerprint,
                "security_patch": payload.security_patch,
                "platform": payload.platform,
            },
            "packages.json": payload.packages,
            "disabled-packages.json": payload.disabled_packages,
            "permissions.json": {
                "packages": {
                    str(package.get("name", "unknown")): package.get(
                        "requested_permissions", []
                    )
                    for package in payload.packages
                }
            },
            "components.json": {"package_summary": payload.package_summary},
            "launcher.json": {"package": payload.launcher},
            "settings.json": payload.settings,
            "processes.json": payload.processes,
            "memory.json": {"summary": payload.memory_summary},
            "storage.json": {"summary": payload.storage_summary},
            "network-summary.json": {
                "summary": payload.network_summary,
                "credentials_collected": False,
            },
            "restore-plan.json": {
                "scope": "inventory-derived plan",
                "automatic_restore": False,
                "operations": [],
            },
            "privacy-report.json": {
                "excluded": [
                    "passwords",
                    "tokens",
                    "cookies",
                    "wifi_credentials",
                    "message_contents",
                ]
            },
        }
        for name, value in artifacts.items():
            _json_write(temp / name, value)
        (temp / "recovery-plan.md").write_text(
            "# Recovery plan\n\n"
            "This is an inventory snapshot and restore plan, not a firmware backup.\n"
            "APK archives are separate. Factory reset is a manual last resort.\n"
            "Full firmware imaging is unsupported.\n",
            encoding="utf-8",
        )
        _write_checksums(temp)
        verify_snapshot(temp)
        if final.exists():
            raise SnapshotError("snapshot timestamp collision")
        os.replace(temp, final)
        return final
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise


def list_snapshots(root: Path) -> list[Path]:
    if not root.exists():
        return []
    ready: list[Path] = []
    for device in root.iterdir():
        if not device.is_dir() or device.name.startswith(".") or device.is_symlink():
            continue
        for candidate in device.iterdir():
            if candidate.name.startswith(".") or not candidate.is_dir() or candidate.is_symlink():
                continue
            try:
                verify_snapshot(candidate)
            except (OSError, SnapshotError, json.JSONDecodeError):
                continue
            ready.append(candidate)
    return sorted(ready)


def inspect_snapshot(path: Path) -> dict[str, Any]:
    return verify_snapshot(path)
