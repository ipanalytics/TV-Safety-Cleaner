from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Platform(StrEnum):
    FIRE_OS = "amazon-fire-os"
    GOOGLE_TV = "google-tv"
    ANDROID_TV = "android-tv"
    VEGA_OS = "amazon-vega-os"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PlatformEvidence:
    properties: dict[str, str]
    packages: frozenset[str]
    launcher: str = ""


@dataclass(frozen=True)
class Detection:
    platform: Platform
    confidence: str
    signals: tuple[str, ...]


def parse_properties(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in raw.splitlines():
        if "]: [" not in line or not line.startswith("["):
            continue
        key, value = line[1:].split("]: [", 1)
        result[key] = value[:-1] if value.endswith("]") else value
    return result


def detect_platform(evidence: PlatformEvidence) -> Detection:
    props = {key: value.lower() for key, value in evidence.properties.items()}
    packages = {value.lower() for value in evidence.packages}
    launcher = evidence.launcher.lower()

    candidates: list[tuple[Platform, tuple[str, ...]]] = []
    fire = tuple(
        signal
        for signal, present in (
            ("amazon manufacturer", "amazon" in props.get("ro.product.manufacturer", "")),
            ("fire product", "fire" in props.get("ro.build.product", "")),
            ("amazon package", any(name.startswith("com.amazon.") for name in packages)),
            ("amazon launcher", "amazon" in launcher),
        )
        if present
    )
    if len(fire) >= 2:
        candidates.append((Platform.FIRE_OS, fire))

    vega = tuple(
        signal
        for signal, present in (
            ("vega property", "vega" in props.get("ro.build.version.name", "")),
            ("vega product", "vega" in props.get("ro.product.name", "")),
            ("vega package", any("vega" in name for name in packages)),
        )
        if present
    )
    if len(vega) >= 2:
        candidates.append((Platform.VEGA_OS, vega))

    google = tuple(
        signal
        for signal, present in (
            ("google tv feature", "com.google.android.feature.google_tv" in packages),
            ("google tv launcher", "googletv" in launcher or "google.tv" in launcher),
            ("google tv product", "google tv" in props.get("ro.product.model", "")),
        )
        if present
    )
    if len(google) >= 2:
        candidates.append((Platform.GOOGLE_TV, google))

    android = tuple(
        signal
        for signal, present in (
            ("leanback feature", "android.software.leanback" in packages),
            ("tv launcher", "tvlauncher" in launcher or "leanback" in launcher),
            ("television ui", props.get("ro.build.characteristics", "") == "tv"),
        )
        if present
    )
    if len(android) >= 2:
        candidates.append((Platform.ANDROID_TV, android))

    if len(candidates) != 1:
        signals = tuple(signal for _, found in candidates for signal in found)
        return Detection(Platform.UNKNOWN, "insufficient-or-conflicting", signals)
    platform, signals = candidates[0]
    return Detection(platform, "multi-signal", signals)
