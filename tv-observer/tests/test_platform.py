import pytest

from tv_observer.platform import Platform, PlatformEvidence, detect_platform


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        (
            PlatformEvidence(
                {"ro.product.manufacturer": "Amazon", "ro.build.product": "firetv"},
                frozenset({"com.amazon.device.messaging"}),
            ),
            Platform.FIRE_OS,
        ),
        (
            PlatformEvidence(
                {"ro.product.name": "vega", "ro.build.version.name": "Vega OS 1"},
                frozenset(),
            ),
            Platform.VEGA_OS,
        ),
        (
            PlatformEvidence(
                {},
                frozenset({"com.google.android.feature.google_tv"}),
                "com.google.android.apps.tv.launcherx.googletv",
            ),
            Platform.GOOGLE_TV,
        ),
        (
            PlatformEvidence(
                {"ro.build.characteristics": "tv"},
                frozenset({"android.software.leanback"}),
            ),
            Platform.ANDROID_TV,
        ),
    ],
)
def test_multi_signal_detection(evidence: PlatformEvidence, expected: Platform) -> None:
    assert detect_platform(evidence).platform is expected


def test_single_or_conflicting_signal_is_unknown() -> None:
    single = PlatformEvidence({"ro.product.manufacturer": "Amazon"}, frozenset())
    assert detect_platform(single).platform is Platform.UNKNOWN

    conflicting = PlatformEvidence(
        {"ro.product.manufacturer": "Amazon", "ro.build.product": "firetv"},
        frozenset({"android.software.leanback"}),
        "tvlauncher",
    )
    assert detect_platform(conflicting).platform is Platform.UNKNOWN
