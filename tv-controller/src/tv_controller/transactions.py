from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from tv_controller.planner import DryRunPlan, PackageCategory, PlannedOperation
from tv_controller.snapshot import ControllerRefusal

HEALTH_CHECKS = ("adb", "launcher", "settings", "network")


class MutationAdapter(Protocol):
    def apply(self, operation: PlannedOperation) -> None: ...

    def health(self, check: str) -> bool: ...

    def revert(self, operation: PlannedOperation) -> None: ...


@dataclass(frozen=True)
class TransactionResult:
    attempted: int
    completed: int
    stopped: bool
    reason: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


class OperationJournal:
    def __init__(self, database: Path) -> None:
        self.database = database
        database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS plans (
                    id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL, retry_allowed INTEGER NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    id INTEGER PRIMARY KEY, plan_id TEXT NOT NULL, package TEXT NOT NULL,
                    action TEXT NOT NULL, category TEXT NOT NULL, status TEXT NOT NULL,
                    health_json TEXT NOT NULL, error TEXT NOT NULL, created_at TEXT NOT NULL,
                    reverted_at TEXT
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        return connection

    def begin(self, plan_id: str, plan: DryRunPlan) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO plans VALUES (?, ?, ?, 'running', 1, ?)",
                    (plan_id, plan.profile_id, plan.snapshot_fingerprint, _now()),
                )
        except sqlite3.IntegrityError as exc:
            raise ControllerRefusal(
                "plan identifier already exists; automatic retry prohibited"
            ) from exc

    def record(
        self,
        plan_id: str,
        operation: PlannedOperation,
        status: str,
        health: dict[str, bool],
        error: str = "",
        reverted: bool = False,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO operations
                (plan_id, package, action, category, status, health_json, error,
                 created_at, reverted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    operation.package,
                    operation.action,
                    operation.category.value,
                    status,
                    json.dumps(health, sort_keys=True),
                    error,
                    _now(),
                    _now() if reverted else None,
                ),
            )

    def finish(self, plan_id: str, status: str, retry_allowed: bool) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE plans SET status = ?, retry_allowed = ? WHERE id = ?",
                (status, int(retry_allowed), plan_id),
            )

    def rollback_candidates(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, plan_id, package, action, category FROM operations
                WHERE status = 'applied' AND reverted_at IS NULL ORDER BY id DESC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def mark_reverted(self, operation_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE operations SET reverted_at = ? WHERE id = ? AND reverted_at IS NULL",
                (_now(), operation_id),
            )

    def report(self) -> dict[str, object]:
        with self._connect() as connection:
            plans = [
                dict(row) for row in connection.execute("SELECT * FROM plans ORDER BY created_at")
            ]
            operations = [
                dict(row) for row in connection.execute("SELECT * FROM operations ORDER BY id")
            ]
        return {"plans": plans, "operations": operations}


class TransactionEngine:
    def __init__(self, journal: OperationJournal, adapter: MutationAdapter) -> None:
        self.journal = journal
        self.adapter = adapter

    def execute(self, plan_id: str, plan: DryRunPlan) -> TransactionResult:
        self.journal.begin(plan_id, plan)
        completed = 0
        for operation in plan.operations:
            self.adapter.apply(operation)
            health: dict[str, bool] = {}
            for check in HEALTH_CHECKS:
                health[check] = self.adapter.health(check)
                if not health[check]:
                    self.adapter.revert(operation)
                    self.journal.record(
                        plan_id,
                        operation,
                        "failed",
                        health,
                        error=f"health check failed: {check}",
                        reverted=True,
                    )
                    self.journal.finish(plan_id, "failed", retry_allowed=False)
                    return TransactionResult(1 + completed, completed, True, f"failed: {check}")
            self.journal.record(plan_id, operation, "applied", health)
            completed += 1
        self.journal.finish(plan_id, "complete", retry_allowed=False)
        return TransactionResult(completed, completed, False, "complete")

    def rollback(self) -> int:
        count = 0
        for row in self.journal.rollback_candidates():
            operation = PlannedOperation(
                package=str(row["package"]),
                category=_category(str(row["category"])),
                action=str(row["action"]),
            )
            self.adapter.revert(operation)
            operation_id = row["id"]
            if not isinstance(operation_id, int):
                raise ControllerRefusal("journal operation identifier is invalid")
            self.journal.mark_reverted(operation_id)
            count += 1
        return count


def _category(value: str) -> PackageCategory:
    return PackageCategory(value)


def rollback_dry_run(journal: OperationJournal) -> dict[str, object]:
    return {"mode": "dry-run", "controller_recorded_operations": journal.rollback_candidates()}


def restore_dry_run(journal: OperationJournal) -> dict[str, object]:
    return {"mode": "dry-run", "journal": journal.report(), "external_changes": "excluded"}
