from __future__ import annotations

import re
from pathlib import Path

import pytest
from werkzeug.security import check_password_hash, generate_password_hash

from tv_observer.web import SECTIONS, create_app

TEST_SECRET = "test-secret-with-enough-entropy"  # noqa: S105
TEST_PASSWORD = "correct horse battery staple"  # noqa: S105


@pytest.fixture
def app(tmp_path: Path):
    application = create_app(
        secret_key=TEST_SECRET,
        password_hash=generate_password_hash(TEST_PASSWORD),
        data_root=tmp_path / "observer",
        snapshot_root=tmp_path / "snapshots",
        device_reader=lambda: [],
    )
    application.config["TESTING"] = True
    return application


def csrf(client, path: str = "/login") -> str:
    response = client.get(path)
    match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
    assert match
    return match.group(1).decode()


def login(client) -> None:
    token = csrf(client)
    response = client.post(
        "/login",
        data={"csrf_token": token, "password": TEST_PASSWORD},
    )
    assert response.status_code == 302


def test_auth_csrf_and_all_sections_render(app) -> None:
    client = app.test_client()
    assert client.get("/").status_code == 302
    assert client.get("/suite-static/suite.css").status_code == 200
    assert client.post("/login", data={"password": "x"}).status_code == 400
    login(client)
    for section in SECTIONS:
        response = client.get(f"/section/{section.lower().replace(' ', '-')}")
        assert response.status_code == 200
        assert section.encode() in response.data
        assert b"TV Safety" in response.data
    assert client.post("/observation/start", data={"name": "x"}).status_code == 400


def test_cidr_guards(app) -> None:
    client = app.test_client()
    assert client.get("/login", environ_base={"REMOTE_ADDR": "192.0.2.10"}).status_code == 403
    lan = create_app(
        secret_key=TEST_SECRET,
        password_hash=generate_password_hash(TEST_PASSWORD),
        data_root=Path(app.instance_path) / "lan-data",
        allow_lan=True,
        trusted_cidrs=("192.0.2.0/24",),
    )
    lan.config["TESTING"] = True
    response = lan.test_client().get(
        "/login", environ_base={"REMOTE_ADDR": "192.0.2.10"}
    )
    assert response.status_code == 200
    with pytest.raises(ValueError, match="trusted CIDR"):
        create_app(
            secret_key=TEST_SECRET,
            password_hash=generate_password_hash(TEST_PASSWORD),
            data_root=Path("x"),
            allow_lan=True,
            trusted_cidrs=(),
        )


def test_dns_collection_is_not_part_of_the_product(app) -> None:
    client = app.test_client()
    login(client)
    token = csrf(client, "/section/diagnostics")
    assert client.get("/section/dns").status_code == 404
    for path in ("/dns/import", "/dns/pull", "/dns/clear"):
        assert client.post(path, data={"csrf_token": token}).status_code == 404


def test_routes_have_no_user_command_parameter(app) -> None:
    rules = {rule.rule: sorted(rule.methods or ()) for rule in app.url_map.iter_rules()}
    assert all("command" not in rule and "adb" not in rule for rule in rules)
    assert "POST" in rules["/observation/start"]
    assert "POST" in rules["/snapshot/capture"]
    assert not any(rule.startswith("/dns/") for rule in rules)


def test_password_change_updates_shared_credential_file(tmp_path: Path) -> None:
    credential_file = tmp_path / "observer.env"
    credential_file.write_text(
        f"TV_OBSERVER_PASSWORD_HASH={generate_password_hash(TEST_PASSWORD)}\n",
        encoding="utf-8",
    )
    application = create_app(
        secret_key=TEST_SECRET,
        password_hash="unused",  # noqa: S106
        credential_file=credential_file,
        data_root=tmp_path / "observer",
        device_reader=lambda: [],
    )
    application.config["TESTING"] = True
    client = application.test_client()
    login(client)
    token = csrf(client, "/section/settings")
    response = client.post(
        "/settings/password",
        data={
            "csrf_token": token,
            "current_password": TEST_PASSWORD,
            "new_password": "654321",
            "confirm_password": "654321",
        },
    )
    assert response.status_code == 302
    stored = credential_file.read_text(encoding="utf-8").split("=", 1)[1].strip()
    assert check_password_hash(stored, "654321")
    assert credential_file.stat().st_mode & 0o777 == 0o600
