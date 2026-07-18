from __future__ import annotations

import json
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from tv_observer.snapshot import SnapshotError, verify_snapshot

SENSITIVE_KEYS = re.compile(
    r"serial|mac|ip(?:_address)?|ssid|email|account|token|cookie|unique|identifier",
    re.IGNORECASE,
)
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
MAC = re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")
IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _load(path: Path, name: str) -> Any:
    return json.loads((path / name).read_text(encoding="utf-8"))


def _package_map(value: object) -> dict[str, dict[str, object]]:
    if not isinstance(value, list):
        return {}
    result: dict[str, dict[str, object]] = {}
    for item in value:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            result[item["name"]] = item
    return result


def diff_snapshots(before: Path, after: Path) -> dict[str, object]:
    verify_snapshot(before)
    verify_snapshot(after)
    old_firmware = _load(before, "firmware.json")
    new_firmware = _load(after, "firmware.json")
    old_packages = _package_map(_load(before, "packages.json"))
    new_packages = _package_map(_load(after, "packages.json"))
    common = old_packages.keys() & new_packages.keys()
    return {
        "firmware": {
            key: {"before": old_firmware.get(key), "after": new_firmware.get(key)}
            for key in ("version", "fingerprint", "security_patch", "platform")
            if old_firmware.get(key) != new_firmware.get(key)
        },
        "packages_added": sorted(new_packages.keys() - old_packages.keys()),
        "packages_removed": sorted(old_packages.keys() - new_packages.keys()),
        "packages_changed": {
            name: {"before": old_packages[name], "after": new_packages[name]}
            for name in sorted(common)
            if old_packages[name] != new_packages[name]
        },
        "disabled_packages": {
            "before": _load(before, "disabled-packages.json"),
            "after": _load(after, "disabled-packages.json"),
        },
        "launcher": {
            "before": _load(before, "launcher.json"),
            "after": _load(after, "launcher.json"),
        },
        "settings": {
            "before": _load(before, "settings.json"),
            "after": _load(after, "settings.json"),
        },
        "processes": {
            "before": _load(before, "processes.json"),
            "after": _load(after, "processes.json"),
        },
    }


def format_diff(diff: dict[str, object]) -> str:
    lines = ["Snapshot differences"]
    for key, value in diff.items():
        if value and not (isinstance(value, dict) and value.get("before") == value.get("after")):
            lines.append(f"- {key}: {json.dumps(value, sort_keys=True)}")
    return "\n".join(lines)


def archive_snapshot(path: Path, output: Path | None = None) -> Path:
    directory = path.resolve(strict=True)
    verify_snapshot(directory)
    destination = output or directory.with_suffix(".zip")
    if destination.exists():
        raise SnapshotError("archive destination already exists")
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in sorted(directory.iterdir()):
            if not item.is_file() or item.is_symlink():
                raise SnapshotError("snapshot contains unsupported entry")
            archive.write(item, arcname=f"{directory.name}/{item.name}")
    with zipfile.ZipFile(destination) as archive:
        for name in archive.namelist():
            target = Path(name)
            if target.is_absolute() or ".." in target.parts:
                destination.unlink(missing_ok=True)
                raise SnapshotError("unsafe archive member")
        if archive.testzip() is not None:
            destination.unlink(missing_ok=True)
            raise SnapshotError("archive integrity check failed")
    return destination


def _mask_text(value: str) -> str:
    value = EMAIL.sub("[REDACTED_EMAIL]", value)
    value = MAC.sub("[REDACTED_MAC]", value)
    value = IP.sub("[REDACTED_IP]", value)
    if "://" in value:
        parsed = urlsplit(value)
        if parsed.query:
            value = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "[REDACTED_QUERY]", ""))
    return value


def _redact(value: object, key: str = "") -> object:
    if key and SENSITIVE_KEYS.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {item_key: _redact(item, item_key) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _mask_text(value)
    return value


def redact_snapshot(path: Path, output: Path | None = None) -> Path:
    source = path.resolve(strict=True)
    verify_snapshot(source)
    destination = output or source.with_name(f"{source.name}-redacted")
    if destination.exists():
        raise SnapshotError("redacted destination already exists")
    shutil.copytree(source, destination, symlinks=False)
    try:
        for item in destination.glob("*.json"):
            data = json.loads(item.read_text(encoding="utf-8"))
            item.write_text(
                json.dumps(_redact(data), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        from tv_observer.snapshot import _write_checksums

        _write_checksums(destination)
        verify_snapshot(destination)
        return destination
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
