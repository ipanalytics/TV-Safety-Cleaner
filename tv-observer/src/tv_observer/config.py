from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ServerConfig:
    host: str = "0.0.0.0"  # noqa: S104 - LAN clients are filtered by trusted CIDRs.
    port: int = 8090


def load_server_config(path: Path | None = None) -> ServerConfig:
    if path is None or not path.exists():
        return ServerConfig()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    server = data.get("server", {})
    return ServerConfig(
        host=str(server.get("host", "0.0.0.0")),  # noqa: S104
        port=int(server.get("port", 8090)),
    )
