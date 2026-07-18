from __future__ import annotations

from pathlib import Path

import pytest
from test_controller_snapshot import make_profile, make_snapshot
from test_planner import add_packages

from tv_controller.planner import PlannedOperation, build_dry_run
from tv_controller.profile import load_profile
from tv_controller.snapshot import ControllerRefusal, load_snapshot
from tv_controller.transactions import (
    HEALTH_CHECKS,
    OperationJournal,
    TransactionEngine,
    restore_dry_run,
    rollback_dry_run,
)


class FakeMutationAdapter:
    def __init__(self, failed_check: str | None = None) -> None:
        self.failed_check = failed_check
        self.calls: list[tuple[str, str]] = []

    def apply(self, operation: PlannedOperation) -> None:
        self.calls.append(("apply", operation.package))

    def health(self, check: str) -> bool:
        self.calls.append(("health", check))
        return check != self.failed_check

    def revert(self, operation: PlannedOperation) -> None:
        self.calls.append(("revert", operation.package))


def plan(tmp_path: Path, packages: list[str]):
    profile_path = make_profile(tmp_path)
    add_packages(profile_path, packages)
    return build_dry_run(
        load_profile(profile_path),
        load_snapshot(make_snapshot(tmp_path)),
        connection_verified=True,
        authorization_verified=True,
    )


@pytest.mark.parametrize("failed_check", HEALTH_CHECKS)
def test_failed_health_stops_reverts_and_disables_retry(tmp_path: Path, failed_check: str) -> None:
    journal = OperationJournal(tmp_path / "journal.sqlite3")
    adapter = FakeMutationAdapter(failed_check)
    engine = TransactionEngine(journal, adapter)
    dry_plan = plan(tmp_path, ["one", "two"])
    result = engine.execute("plan-1", dry_plan)
    assert result.stopped is True
    assert result.completed == 0
    assert ("apply", "two") not in adapter.calls
    assert ("revert", "one") in adapter.calls
    report = journal.report()
    assert report["plans"][0]["retry_allowed"] == 0
    assert report["operations"][0]["status"] == "failed"
    with pytest.raises(ControllerRefusal, match="automatic retry prohibited"):
        engine.execute("plan-1", dry_plan)


def test_one_operation_before_each_health_gate(tmp_path: Path) -> None:
    journal = OperationJournal(tmp_path / "journal.sqlite3")
    adapter = FakeMutationAdapter()
    result = TransactionEngine(journal, adapter).execute("plan-1", plan(tmp_path, ["one", "two"]))
    assert result.completed == 2
    first_apply = adapter.calls.index(("apply", "one"))
    second_apply = adapter.calls.index(("apply", "two"))
    between = adapter.calls[first_apply + 1 : second_apply]
    assert between == [("health", check) for check in HEALTH_CHECKS]


def test_rollback_only_journal_entries_and_is_idempotent(tmp_path: Path) -> None:
    journal = OperationJournal(tmp_path / "journal.sqlite3")
    adapter = FakeMutationAdapter()
    engine = TransactionEngine(journal, adapter)
    engine.execute("plan-1", plan(tmp_path, ["one"]))
    preview = rollback_dry_run(journal)
    assert [item["package"] for item in preview["controller_recorded_operations"]] == ["one"]
    assert engine.rollback() == 1
    assert engine.rollback() == 0
    assert adapter.calls.count(("revert", "one")) == 1
    assert rollback_dry_run(journal)["controller_recorded_operations"] == []
    assert restore_dry_run(journal)["external_changes"] == "excluded"
