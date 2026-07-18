from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOYER = ROOT / "tv.sh"


def test_root_deployer_bundle_check() -> None:
    result = subprocess.run(  # noqa: S603
        ["bash", str(DEPLOYER), "--check"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Bundle structure and shell syntax are valid" in result.stdout


def test_root_readme_documents_real_deployment_and_compatibility_boundaries() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for required in (
        "sudo bash ./tv.sh",
        "http://<raspberry-pi-ip>:8090",
        "http://<raspberry-pi-ip>:8091",
        "Android TV",
        "Google TV",
        "Amazon Fire TV / Fire OS",
        "Samsung Tizen, LG webOS, Roku OS, VIDAA, Apple TV",
        "Journal and Selective Rollback",
        "Named State Profiles",
    ):
        assert required in readme


def test_deployer_preserves_private_state_boundaries() -> None:
    deployer = DEPLOYER.read_text(encoding="utf-8")
    assert '"$BUNDLE_DIR/README.md"' in deployer
    assert "/srv/tv-safety-data" in deployer
    assert "TV_OBSERVER_ADMIN_PASSWORD" in deployer
    assert "at least 6 characters" in deployer
    assert "TV_OBSERVER_ALLOW_LAN=true" in deployer
    assert "192.168.0.0/16" in deployer
    assert 'SHARED_SOURCE="$BUNDLE_DIR/tv-shared"' in deployer
    assert 'SHARED_DESTINATION="/srv/tv-shared"' in deployer
    assert "ensure_service_identity" in deployer
    assert "cleanup_retired_features" in deployer
    assert "cleanup_uploaded_bundle" in deployer
    assert "adguard-settings.json" in deployer
    assert "retired_imports" in deployer
    assert "rm " not in deployer
    for forbidden in (
        "/srv/pi-media-stack",
        "/srv/media",
        "/srv/media-disk",
        "/srv/downloads",
        "minidlna",
        "qbittorrent",
    ):
        assert forbidden not in deployer.lower()

    for project in ("tv-observer", "tv-controller"):
        installer = (ROOT / project / "scripts/install-raspberry.sh").read_text(encoding="utf-8")
        assert "rsync -a --delete" in installer
        assert "--exclude '.venv/'" in installer
        assert 'pip" install --no-deps --editable /srv/tv-shared' in installer
        assert 'pip\" install --no-deps --editable "$APP_DIR"' in installer
        assert f"case \"$APP_DIR\" in /srv/{project}" in installer

    controller_unit = (ROOT / "tv-controller/systemd/tv-controller.service").read_text(
        encoding="utf-8"
    )
    assert "EnvironmentFile=/srv/tv-safety-data/observer/observer.env" in controller_unit
    assert "--bind 0.0.0.0:8091" in controller_unit
    assert "Environment=HOME=/srv/tv-safety-data/controller" in controller_unit
    assert "Environment=ANDROID_USER_HOME=/srv/tv-safety-data/controller" in controller_unit
    assert "UMask=0077" in controller_unit


def test_controller_deploy_preserves_state_and_discards_ephemeral_activity() -> None:
    installer = (ROOT / "tv-controller/scripts/install-raspberry.sh").read_text(
        encoding="utf-8"
    )
    assert '"$DATA_ROOT/controller/profiles"' in installer
    assert 'systemctl stop "$SERVICE"' in installer
    assert (
        'find "$DATA_ROOT/controller/apks/_tasks" -xdev -type f -name \'*.json\' -delete'
        in installer
    )
    assert "operations.sqlite3" not in installer
