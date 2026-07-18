from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tv_controller.snapshot import ControllerRefusal

PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class DeviceIdentity:
    model: str
    platform: str
    firmware: str
    fingerprint: str


@dataclass(frozen=True)
class PackageState:
    installed: bool
    enabled: bool
    third_party: bool
    version_code: int = 0
    version_name: str = "unknown"


@dataclass(frozen=True)
class OperationRecord:
    id: int
    package: str
    action: str
    status: str
    inverse_action: str
    before: PackageState
    after: PackageState | None
    device: DeviceIdentity
    before_apk: str
    after_apk: str
    batch_id: str
    parent_id: int | None
    created_at: str
    completed_at: str | None
    error: str


class StateJournal:
    """Durable package-state ledger with conflict-aware selective rollback."""

    def __init__(self, database: Path) -> None:
        self.database = database
        database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS state_operations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    package TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    inverse_action TEXT NOT NULL DEFAULT '',
                    before_json TEXT NOT NULL,
                    after_json TEXT,
                    device_json TEXT NOT NULL,
                    before_apk TEXT NOT NULL DEFAULT '',
                    after_apk TEXT NOT NULL DEFAULT '',
                    batch_id TEXT NOT NULL DEFAULT '',
                    parent_id INTEGER,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS state_operations_package_id
                    ON state_operations(package, id);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        return connection

    def begin(
        self,
        *,
        package: str,
        action: str,
        before: PackageState,
        device: DeviceIdentity,
        before_apk: str = "",
        after_apk: str = "",
        batch_id: str = "",
        parent_id: int | None = None,
        inverse_action: str = "",
    ) -> int:
        with self._connect() as connection:
            # Serialize state-branch creation so rapid requests cannot share a stale before-state.
            connection.execute("BEGIN IMMEDIATE")
            unresolved = connection.execute(
                """
                SELECT id FROM state_operations
                WHERE package = ? AND status IN ('pending', 'uncertain')
                ORDER BY id DESC LIMIT 1
                """,
                (package,),
            ).fetchone()
            if unresolved is not None and int(unresolved["id"]) != parent_id:
                raise ControllerRefusal(
                    "this package already has an unfinished or uncertain operation"
                )
            cursor = connection.execute(
                """
                INSERT INTO state_operations
                (package, action, status, inverse_action, before_json, device_json, before_apk,
                 after_apk, batch_id, parent_id, created_at)
                VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    package,
                    action,
                    inverse_action,
                    json.dumps(asdict(before), sort_keys=True),
                    json.dumps(asdict(device), sort_keys=True),
                    before_apk,
                    after_apk,
                    batch_id,
                    parent_id,
                    _now(),
                ),
            )
            operation_id = cursor.lastrowid
        if not isinstance(operation_id, int):
            raise ControllerRefusal("operation journal did not allocate an identifier")
        return operation_id

    def complete(self, operation_id: int, after: PackageState, inverse_action: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE state_operations
                SET status = 'applied', after_json = ?, inverse_action = ?, completed_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (json.dumps(asdict(after), sort_keys=True), inverse_action, _now(), operation_id),
            )
            if cursor.rowcount != 1:
                raise ControllerRefusal("operation journal transition is invalid")

    def fail(self, operation_id: int, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE state_operations SET status = 'uncertain', error = ?, completed_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (error[:500], _now(), operation_id),
            )

    def get(self, operation_id: int) -> OperationRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM state_operations WHERE id = ?", (operation_id,)
            ).fetchone()
        if row is None:
            raise ControllerRefusal("operation does not exist")
        return self._record(row)

    def list(self, limit: int = 200) -> list[OperationRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM state_operations ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._record(row) for row in rows]

    def prepare_selective_rollback(
        self,
        operation_id: int,
        current: PackageState,
        device: DeviceIdentity,
    ) -> OperationRecord:
        """Validate that only the selected package branch can be safely restored."""

        record = self.get(operation_id)
        if record.status not in {"applied", "uncertain"} or not record.inverse_action:
            raise ControllerRefusal("operation is not available for rollback")
        if record.device != device:
            raise ControllerRefusal("device identity changed; rollback refused")
        with self._connect() as connection:
            newer = connection.execute(
                """
                SELECT id FROM state_operations
                WHERE package = ? AND id > ? AND status IN ('pending', 'applied', 'uncertain')
                LIMIT 1
                """,
                (record.package, record.id),
            ).fetchone()
        if newer is not None:
            raise ControllerRefusal(
                "a newer active operation changed this package; rollback would violate its state"
            )
        if record.status == "applied" and current != record.after:
            raise ControllerRefusal(
                "current package state differs from the recorded result; refresh evidence first"
            )
        return record

    def reconcile_no_change(self, operation_id: int) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE state_operations
                SET status = 'reconciled', completed_at = ?, error = ''
                WHERE id = ? AND status = 'uncertain'
                """,
                (_now(), operation_id),
            )
            if cursor.rowcount != 1:
                raise ControllerRefusal("uncertain operation is no longer reconcilable")

    def finish_rollback(self, original_id: int, rollback_id: int) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE state_operations SET status = 'reverted', completed_at = ?
                WHERE id = ? AND status IN ('applied', 'uncertain')
                """,
                (_now(), original_id),
            )
            if cursor.rowcount != 1:
                raise ControllerRefusal("original operation is no longer rollbackable")
            rollback = connection.execute(
                "SELECT status FROM state_operations WHERE id = ?", (rollback_id,)
            ).fetchone()
            if rollback is None or rollback["status"] != "applied":
                raise ControllerRefusal("rollback operation was not completed")
            connection.execute(
                # The rollback event is evidence, not a new active package-state branch.
                "UPDATE state_operations SET status = 'rollback-recorded' WHERE id = ?",
                (rollback_id,),
            )

    @staticmethod
    def _record(row: sqlite3.Row) -> OperationRecord:
        before_raw = json.loads(str(row["before_json"]))
        after_raw = json.loads(str(row["after_json"])) if row["after_json"] else None
        device_raw = json.loads(str(row["device_json"]))
        return OperationRecord(
            id=int(row["id"]),
            package=str(row["package"]),
            action=str(row["action"]),
            status=str(row["status"]),
            inverse_action=str(row["inverse_action"]),
            before=PackageState(**before_raw),
            after=PackageState(**after_raw) if after_raw is not None else None,
            device=DeviceIdentity(**device_raw),
            before_apk=str(row["before_apk"]),
            after_apk=str(row["after_apk"]),
            batch_id=str(row["batch_id"]),
            parent_id=int(row["parent_id"]) if row["parent_id"] is not None else None,
            created_at=str(row["created_at"]),
            completed_at=str(row["completed_at"]) if row["completed_at"] else None,
            error=str(row["error"]),
        )


@dataclass(frozen=True)
class ProfilePackage:
    package: str
    enabled: bool
    version_code: int
    version_name: str
    apk_file: str


@dataclass(frozen=True)
class StateProfile:
    id: str
    name: str
    device: DeviceIdentity
    packages: tuple[ProfilePackage, ...]
    created_at: str

    @property
    def complete(self) -> bool:
        return all(item.apk_file for item in self.packages)


class StateProfileStore:
    """Store exact-build, exact-version user-package profiles as private JSON files."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def save(
        self, name: str, device: DeviceIdentity, packages: list[ProfilePackage]
    ) -> StateProfile:
        normalized = self.validate_name(name)
        profile_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        suffix = 1
        while (self.root / f"{profile_id}.json").exists():
            suffix += 1
            profile_id = f"{profile_id.rsplit('-', 1)[0]}-{suffix}"
        profile = StateProfile(profile_id, normalized, device, tuple(packages), _now())
        payload = {
            "schema": 1,
            "id": profile.id,
            "name": profile.name,
            "device": asdict(profile.device),
            "packages": [asdict(item) for item in profile.packages],
            "created_at": profile.created_at,
        }
        temporary = self.root / f".{profile.id}.tmp"
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.root / f"{profile.id}.json")
        return profile

    @staticmethod
    def validate_name(name: str) -> str:
        normalized = name.strip()
        if not PROFILE_NAME.fullmatch(normalized):
            raise ControllerRefusal("profile name must be 1-64 safe characters")
        return normalized

    def get(self, profile_id: str) -> StateProfile:
        if not re.fullmatch(r"[0-9TZ-]+", profile_id):
            raise ControllerRefusal("profile identifier is invalid")
        path = self.root / f"{profile_id}.json"
        if not path.is_file() or path.is_symlink():
            raise ControllerRefusal("profile does not exist")
        try:
            data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            if data.get("schema") != 1:
                raise ValueError("schema")
            return StateProfile(
                id=str(data["id"]),
                name=str(data["name"]),
                device=DeviceIdentity(**data["device"]),
                packages=tuple(ProfilePackage(**item) for item in data["packages"]),
                created_at=str(data["created_at"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ControllerRefusal("profile data is invalid") from exc

    def list(self) -> list[StateProfile]:
        profiles: list[StateProfile] = []
        for path in self.root.glob("*.json"):
            try:
                profiles.append(self.get(path.stem))
            except ControllerRefusal:
                continue
        return sorted(profiles, key=lambda item: item.created_at, reverse=True)
