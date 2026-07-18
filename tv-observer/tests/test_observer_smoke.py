from tv_observer.config import ServerConfig


def test_default_port() -> None:
    config = ServerConfig()
    assert config.host == "0.0.0.0"  # noqa: S104
    assert config.port == 8090
