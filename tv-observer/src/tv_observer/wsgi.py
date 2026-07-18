from __future__ import annotations

import os
from pathlib import Path

from tv_observer.web import create_app


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


app = create_app(
    secret_key=_required("TV_OBSERVER_SECRET_KEY"),
    password_hash=_required("TV_OBSERVER_PASSWORD_HASH"),
    data_root=Path(os.environ.get("TV_OBSERVER_DATA_ROOT", "/srv/tv-safety-data/observer")),
    snapshot_root=Path(
        os.environ.get("TV_OBSERVER_SNAPSHOT_ROOT", "/srv/tv-safety-data/observer/snapshots")
    ),
    credential_file=Path("/srv/tv-safety-data/observer/observer.env"),
    allow_lan=os.environ.get("TV_OBSERVER_ALLOW_LAN", "false").lower() == "true",
    trusted_cidrs=tuple(
        item.strip()
        for item in os.environ.get("TV_OBSERVER_TRUSTED_CIDRS", "127.0.0.0/8,::1/128").split(",")
        if item.strip()
    ),
    cookie_secure=os.environ.get("TV_OBSERVER_COOKIE_SECURE", "false").lower() == "true",
)
