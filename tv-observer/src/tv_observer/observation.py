from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from tv_observer.adb import AdbError

SCENARIOS = frozenset({"idle", "streaming", "app-launch", "standby", "live-tv", "custom"})
FOREGROUND_PACKAGE = re.compile(
    r"(?:mResumedActivity|topResumedActivity).*? ([A-Za-z0-9._]+)/"
)


class ObservationError(RuntimeError):
    """Observation session transition is invalid."""


@dataclass(frozen=True)
class Sample:
    adb_available: bool
    foreground_package: str
    ram_summary: str
    cpu_summary: str
    processes: tuple[str, ...]
    storage_summary: str
    recorded_at: str = ""


class ObservationReader(Protocol):
    def activity_summary(self) -> str: ...

    def memory_summary(self) -> str: ...

    def cpu_summary(self) -> str: ...

    def process_summary(self) -> str: ...

    def storage_summary(self) -> str: ...


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ObservationStore:
    def __init__(self, database: Path, poll_interval_seconds: int = 30) -> None:
        if poll_interval_seconds < 5:
            raise ObservationError("poll interval must be at least 5 seconds")
        self.database = database
        self.poll_interval_seconds = poll_interval_seconds
        database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL, started_at TEXT NOT NULL,
                    stopped_at TEXT, poll_interval INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scenarios (
                    id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, label TEXT NOT NULL,
                    started_at TEXT NOT NULL, stopped_at TEXT
                );
                CREATE TABLE IF NOT EXISTS samples (
                    id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, recorded_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                """
            )

    def _active_session(self, connection: sqlite3.Connection) -> sqlite3.Row | None:
        row = connection.execute(
            "SELECT * FROM sessions WHERE stopped_at IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def start(self, name: str) -> int:
        if not name.strip() or len(name) > 100:
            raise ObservationError("session name is required and limited to 100 characters")
        with self._connect() as connection:
            if self._active_session(connection):
                raise ObservationError("an observation session is already active")
            cursor = connection.execute(
                "INSERT INTO sessions(name, started_at, poll_interval) VALUES (?, ?, ?)",
                (name.strip(), _now(), self.poll_interval_seconds),
            )
            if cursor.lastrowid is None:
                raise ObservationError("session identifier was not created")
            return cursor.lastrowid

    def status(self) -> dict[str, object] | None:
        with self._connect() as connection:
            row = self._active_session(connection)
            return dict(row) if row else None

    def stop(self) -> None:
        with self._connect() as connection:
            row = self._active_session(connection)
            if not row:
                raise ObservationError("no active observation session")
            connection.execute(
                "UPDATE scenarios SET stopped_at = ? WHERE stopped_at IS NULL", (_now(),)
            )
            connection.execute(
                "UPDATE sessions SET stopped_at = ? WHERE id = ?", (_now(), row["id"])
            )

    def scenario_start(self, label: str) -> int:
        if label not in SCENARIOS:
            raise ObservationError("unsupported scenario label")
        with self._connect() as connection:
            session = self._active_session(connection)
            if not session:
                raise ObservationError("no active observation session")
            active = connection.execute(
                "SELECT id FROM scenarios WHERE stopped_at IS NULL"
            ).fetchone()
            if active:
                raise ObservationError("a scenario is already active")
            cursor = connection.execute(
                "INSERT INTO scenarios(session_id, label, started_at) VALUES (?, ?, ?)",
                (session["id"], label, _now()),
            )
            if cursor.lastrowid is None:
                raise ObservationError("scenario identifier was not created")
            return cursor.lastrowid

    def scenario_stop(self) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE scenarios SET stopped_at = ? WHERE stopped_at IS NULL", (_now(),)
            )
            if cursor.rowcount == 0:
                raise ObservationError("no active scenario")

    def add_sample(self, sample: Sample) -> None:
        with self._connect() as connection:
            session = self._active_session(connection)
            if not session:
                raise ObservationError("no active observation session")
            data = asdict(sample)
            data["recorded_at"] = sample.recorded_at or _now()
            connection.execute(
                "INSERT INTO samples(session_id, recorded_at, payload) VALUES (?, ?, ?)",
                (session["id"], data["recorded_at"], json.dumps(data)),
            )

    def report(self, session_id: int | None = None) -> dict[str, object]:
        with self._connect() as connection:
            if session_id is None:
                session = connection.execute(
                    "SELECT * FROM sessions ORDER BY id DESC LIMIT 1"
                ).fetchone()
            else:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
            if not session:
                raise ObservationError("observation session not found")
            scenarios = connection.execute(
                "SELECT label, started_at, stopped_at FROM scenarios "
                "WHERE session_id = ? ORDER BY id",
                (session["id"],),
            ).fetchall()
            samples = connection.execute(
                "SELECT payload FROM samples WHERE session_id = ? ORDER BY id", (session["id"],)
            ).fetchall()
            return {
                "session": dict(session),
                "scenarios": [dict(row) for row in scenarios],
                "samples": [json.loads(row["payload"]) for row in samples],
                "continuous_log_collection": False,
            }


class ObservationPoller:
    def __init__(self, store: ObservationStore, reader: ObservationReader) -> None:
        self.store = store
        self.reader = reader

    def poll_once(self) -> Sample:
        try:
            activity = self.reader.activity_summary()
            match = FOREGROUND_PACKAGE.search(activity)
            sample = Sample(
                adb_available=True,
                foreground_package=match.group(1) if match else "unknown",
                ram_summary=self.reader.memory_summary(),
                cpu_summary=self.reader.cpu_summary(),
                processes=tuple(self.reader.process_summary().splitlines()),
                storage_summary=self.reader.storage_summary(),
            )
        except AdbError as exc:
            sample = Sample(False, "unknown", str(exc), "unavailable", (), "unavailable")
        self.store.add_sample(sample)
        return sample

    def run(
        self,
        *,
        max_samples: int | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> int:
        count = 0
        while self.store.status() is not None and (max_samples is None or count < max_samples):
            self.poll_once()
            count += 1
            if self.store.status() is not None and (max_samples is None or count < max_samples):
                sleeper(self.store.poll_interval_seconds)
        return count
