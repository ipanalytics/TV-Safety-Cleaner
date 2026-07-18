from pathlib import Path

import pytest

from tv_observer.observation import (
    SCENARIOS,
    ObservationError,
    ObservationPoller,
    ObservationStore,
    Sample,
)
from tv_observer.recovery import (
    READINESS_ITEMS,
    ReadinessItem,
    ReadinessStatus,
    assess_readiness,
    default_readiness_report,
    factory_reset_drill_status,
)


def test_observation_lifecycle_and_report(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path / "state.sqlite3", poll_interval_seconds=15)
    session_id = store.start("24 hour baseline")
    assert store.status()["poll_interval"] == 15
    scenario_id = store.scenario_start("streaming")
    assert scenario_id > 0
    store.add_sample(Sample(True, "com.media", "1 GB", "5%", ("media",), "50%"))
    store.scenario_stop()
    store.stop()
    report = store.report(session_id)
    assert report["scenarios"][0]["label"] == "streaming"
    assert report["samples"][0]["foreground_package"] == "com.media"
    assert report["continuous_log_collection"] is False
    assert store.status() is None


def test_scenario_labels_and_polling_limits(tmp_path: Path) -> None:
    assert SCENARIOS == {"idle", "streaming", "app-launch", "standby", "live-tv", "custom"}
    with pytest.raises(ObservationError, match="at least 5"):
        ObservationStore(tmp_path / "state.sqlite3", poll_interval_seconds=1)
    store = ObservationStore(tmp_path / "state.sqlite3")
    store.start("test")
    with pytest.raises(ObservationError, match="unsupported"):
        store.scenario_start("factory-reset")


def test_recovery_status_vocabulary_and_manual_factory_gate() -> None:
    report = assess_readiness(
        [ReadinessItem("ADB reconnect", ReadinessStatus.PARTIAL, "checked after standby")]
    )
    assert report["allowed_statuses"] == [
        "Not checked",
        "Documented only",
        "Partially verified",
        "Verified on this device",
        "Failed",
    ]
    with pytest.raises(ValueError, match="manual drill"):
        factory_reset_drill_status(ReadinessStatus.VERIFIED, "documentation")
    assert (
        factory_reset_drill_status(ReadinessStatus.VERIFIED, "manual-device-drill")
        is ReadinessStatus.VERIFIED
    )
    default = default_readiness_report()
    assert len(READINESS_ITEMS) == 17
    assert [item["name"] for item in default["items"]] == list(READINESS_ITEMS)
    assert {item["status"] for item in default["items"]} == {"Not checked"}


class FakeObservationReader:
    def activity_summary(self) -> str:
        return "mResumedActivity x com.example.media/.Main"

    def memory_summary(self) -> str:
        return "MemTotal: 1024 kB"

    def cpu_summary(self) -> str:
        return "5% com.example.media"

    def process_summary(self) -> str:
        return "PID NAME\n1 init"

    def storage_summary(self) -> str:
        return "/data 50%"


def test_poller_collects_lightweight_samples_without_log_stream(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path / "state.sqlite3", poll_interval_seconds=5)
    store.start("polling")
    sleeps: list[float] = []
    count = ObservationPoller(store, FakeObservationReader()).run(
        max_samples=2, sleeper=sleeps.append
    )
    report = store.report()
    assert count == 2
    assert sleeps == [5]
    assert report["samples"][0]["foreground_package"] == "com.example.media"
    assert report["samples"][0]["cpu_summary"] == "5% com.example.media"
    assert report["continuous_log_collection"] is False
