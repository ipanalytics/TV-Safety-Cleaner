from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SUPPORTED_PLATFORMS = frozenset({"amazon-fire-os", "google-tv", "android-tv", "amazon-vega-os"})


class ControllerRefusal(RuntimeError):
    """A safety gate refused Controller processing."""


@dataclass(frozen=True)
class VerifiedSnapshot:
    path: Path
    manifest: dict[str, Any]
    restore_plan: dict[str, Any]


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def load_snapshot(path: Path) -> VerifiedSnapshot:
    try:
        directory = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ControllerRefusal("snapshot does not exist") from exc
    if not directory.is_dir() or path.is_symlink():
        raise ControllerRefusal("snapshot must be a real directory")
    checksums = directory / "checksums.sha256"
    if not checksums.is_file() or checksums.is_symlink():
        raise ControllerRefusal("snapshot checksum manifest is missing")
    seen: set[str] = set()
    for line in checksums.read_text(encoding="ascii").splitlines():
        try:
            digest, name = line.split("  ", 1)
        except ValueError as exc:
            raise ControllerRefusal("snapshot checksum manifest is malformed") from exc
        if Path(name).name != name or name in seen:
            raise ControllerRefusal("snapshot checksum path is unsafe")
        target = directory / name
        if not target.is_file() or target.is_symlink() or _digest(target) != digest:
            raise ControllerRefusal(f"snapshot checksum invalid for {name}")
        seen.add(name)
    if not {"manifest.json", "restore-plan.json"}.issubset(seen):
        raise ControllerRefusal("snapshot is missing required checked artifacts")
    allowed = seen | {"checksums.sha256"}
    for entry in directory.iterdir():
        if entry.name not in allowed or not entry.is_file() or entry.is_symlink():
            raise ControllerRefusal(f"snapshot has unsupported or unchecked entry: {entry.name}")
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        restore_plan = json.loads((directory / "restore-plan.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControllerRefusal("snapshot JSON is invalid") from exc
    if manifest.get("schema_version") != "1.0" or manifest.get("kind") != "inventory-snapshot":
        raise ControllerRefusal("snapshot schema is unsupported")
    return VerifiedSnapshot(directory, manifest, restore_plan)
