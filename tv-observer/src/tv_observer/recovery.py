from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

READINESS_ITEMS = (
    "model identified",
    "platform identified",
    "firmware identified",
    "baseline snapshot created",
    "snapshot checksum verified",
    "ADB reconnect verified",
    "standard reset documented",
    "physical or remote reset documented",
    "factory reset manually verified",
    "ADB re-enable after reset documented",
    "launcher checked",
    "Settings checked",
    "network checked",
    "HDMI checked manually",
    "tuner checked manually",
    "sound checked manually",
    "OTA checked manually",
)


class ReadinessStatus(StrEnum):
    NOT_CHECKED = "Not checked"
    DOCUMENTED = "Documented only"
    PARTIAL = "Partially verified"
    VERIFIED = "Verified on this device"
    FAILED = "Failed"


@dataclass(frozen=True)
class ReadinessItem:
    name: str
    status: ReadinessStatus
    evidence: str


def assess_readiness(items: list[ReadinessItem]) -> dict[str, object]:
    return {
        "allowed_statuses": [status.value for status in ReadinessStatus],
        "items": [
            {"name": item.name, "status": item.status.value, "evidence": item.evidence}
            for item in items
        ],
    }


def default_readiness_report() -> dict[str, object]:
    return assess_readiness(
        [ReadinessItem(name, ReadinessStatus.NOT_CHECKED, "") for name in READINESS_ITEMS]
    )


def factory_reset_drill_status(
    requested: ReadinessStatus,
    evidence_type: str,
) -> ReadinessStatus:
    if requested is ReadinessStatus.VERIFIED and evidence_type != "manual-device-drill":
        raise ValueError("factory reset verification requires a manual drill on this device")
    return requested
