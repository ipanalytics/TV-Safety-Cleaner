from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen

from tv_controller.apk import MAX_APK_BYTES, inspect_apk
from tv_controller.planner import is_never_touch
from tv_controller.snapshot import ControllerRefusal
from tv_controller.state_history import (
    DeviceIdentity,
    OperationRecord,
    PackageState,
    ProfilePackage,
    StateJournal,
    StateProfile,
    StateProfileStore,
)

DEFAULT_DOWNLOADER_ENDPOINT = (
    "https://online-apk-downloader.com/apk-ajax&packageDownload"
)
PACKAGE_PATTERN = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$")
VERSION_KEYS = (
    "versionCode",
    "version_code",
    "versioncode",
    "appVersionCode",
    "apk_version_code",
)
FAILED_TASK_TTL = 300
POLL_TIMEOUT = 180
READ_CHUNK = 1024 * 1024


def parse_package_input(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        raise ValueError("Package ID is empty")
    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme != "https" or parsed.netloc.lower() != "play.google.com":
            raise ValueError("Only HTTPS Google Play application URLs are accepted")
        values = parse_qs(parsed.query).get("id", [])
        candidate = values[0].strip() if values else ""
    if not PACKAGE_PATTERN.fullmatch(candidate):
        raise ValueError(f"Invalid Android package ID: {value.strip()}")
    return candidate


def parse_package_inputs(value: str) -> list[str]:
    packages: list[str] = []
    for line in value.splitlines():
        if line.strip():
            package = parse_package_input(line)
            if package not in packages:
                packages.append(package)
    if not packages:
        raise ValueError("Enter at least one package ID or Google Play URL")
    return packages


def safe_apk_filename(value: str) -> str:
    name = unquote(value).strip()
    if (
        not name
        or name != Path(name).name
        or "/" in name
        or "\\" in name
        or ".." in name
        or not name.lower().endswith(".apk")
    ):
        raise ControllerRefusal("Unsafe APK filename")
    if not re.fullmatch(r"[A-Za-z0-9._+-]+\.apk", name, re.IGNORECASE):
        raise ControllerRefusal("APK filename contains unsupported characters")
    return name


def version_from_download_data(
    data: dict[str, Any], filename: str | None = None
) -> tuple[int, str]:
    for key in VERSION_KEYS:
        raw = data.get(key)
        if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
            return raw, f"downloader JSON field {key}"
        if isinstance(raw, str) and raw.isdigit():
            return int(raw), f"downloader JSON field {key}"
    candidate = filename or Path(urlparse(str(data.get("downloadUrl", ""))).path).name
    match = re.search(r"-(\d+)(?:-|\.apk$|$)", candidate, re.IGNORECASE)
    if match:
        return int(match.group(1)), "downloader filename"
    return 0, "unknown"


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class ApkSettings:
    downloader_endpoint: str = DEFAULT_DOWNLOADER_ENDPOINT
    adb_host: str = "192.168.0.110:5555"
    sideload_enabled: bool = False


def validate_downloader_endpoint(value: str) -> str:
    endpoint = value.strip().rstrip("?&")
    parsed = urlparse(endpoint)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "online-apk-downloader.com"
        or parsed.path != "/apk-ajax&packageDownload"
        or parsed.query
    ):
        raise ValueError("Downloader endpoint must use the approved HTTPS APK endpoint")
    return endpoint


def validate_adb_host(value: str) -> str:
    candidate = value.strip()
    host, separator, port_text = candidate.rpartition(":")
    if not separator:
        host, port_text = candidate, "5555"
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("ADB host must be an IP address with an optional port") from exc
    if not address.is_private:
        raise ValueError("ADB host must be a private-network IP address")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError("ADB port must be numeric") from exc
    if not 1 <= port <= 65535:
        raise ValueError("ADB port is outside the valid range")
    return f"{address}:{port}"


def task_error_message(value: object) -> str:
    message = str(value).strip()
    normalized = message.lower()
    if any(
        marker in normalized
        for marker in (
            "adb server didn't ack",
            "adb starting",
            "cannot mkdir",
            "cannot connect to daemon",
            "daemon not running",
        )
    ):
        return "Controller could not start its local ADB service. Redeploy with tv.sh."
    if "unauthorized" in normalized:
        return "TV rejected ADB authorization. Confirm the RSA prompt on the TV."
    if "offline" in normalized:
        return "TV is visible but ADB is offline. Re-enable ADB on the TV and retry."
    if not message:
        return "The operation failed without a diagnostic message."
    return message.splitlines()[0][:300]


class ApkSettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> ApkSettings:
        if not self.path.is_file() or self.path.is_symlink():
            return ApkSettings()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("settings must be an object")
            return ApkSettings(
                downloader_endpoint=validate_downloader_endpoint(
                    str(data.get("downloader_endpoint", DEFAULT_DOWNLOADER_ENDPOINT))
                ),
                adb_host=validate_adb_host(str(data.get("adb_host", "192.168.0.110:5555"))),
                sideload_enabled=data.get("sideload_enabled") is True,
            )
        except (OSError, json.JSONDecodeError, ValueError):
            return ApkSettings()

    def save(self, settings: ApkSettings) -> None:
        atomic_write_json(
            self.path,
            {
                "downloader_endpoint": validate_downloader_endpoint(
                    settings.downloader_endpoint
                ),
                "adb_host": validate_adb_host(settings.adb_host),
                "sideload_enabled": settings.sideload_enabled,
            },
        )


class TaskStore:
    def __init__(self, root: Path, clock: Callable[[], float] = time.time) -> None:
        self.root = root
        self.clock = clock
        self.root.mkdir(parents=True, exist_ok=True)

    def new(self, package: str, *, kind: str = "download") -> str:
        now = self.clock()
        task_id = f"{int(now)}-{secrets.token_hex(4)}"
        self.write(
            task_id,
            {
                "id": task_id,
                "kind": kind,
                "package": parse_package_input(package),
                "stage": "queued",
                "status": "Queued",
                "progress": 0,
                "created_at": now,
                "updated_at": now,
            },
        )
        return task_id

    def read(self, task_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9]+-[a-f0-9]{8}", task_id):
            raise ControllerRefusal("Invalid task ID")
        path = self.root / f"{task_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ControllerRefusal("Task data is invalid")
        return data

    def write(self, task_id: str, data: dict[str, Any]) -> None:
        value = dict(data)
        value["id"] = task_id
        value["updated_at"] = self.clock()
        atomic_write_json(self.root / f"{task_id}.json", value)

    def update(self, task_id: str, **changes: Any) -> dict[str, Any]:
        data = self.read(task_id)
        data.update(changes)
        self.write(task_id, data)
        return data

    def active(self) -> list[dict[str, Any]]:
        now = self.clock()
        tasks: list[dict[str, Any]] = []
        for path in self.root.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict) or data.get("stage") == "done":
                continue
            updated = float(data.get("updated_at", 0))
            if data.get("stage") in {"failed", "result"} and now - updated > FAILED_TASK_TTL:
                continue
            tasks.append(data)
        return sorted(tasks, key=lambda item: float(item.get("created_at", 0)), reverse=True)

    def clear_finished(self) -> int:
        removed = 0
        for path in self.root.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and data.get("stage") in {"done", "failed", "result"}:
                path.unlink(missing_ok=True)
                removed += 1
        return removed


class ApkRepository:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.tasks = TaskStore(root / "_tasks")
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, filename: str, *, must_exist: bool = True) -> Path:
        name = safe_apk_filename(filename)
        path = self.root / name
        resolved_root = self.root.resolve()
        resolved = path.resolve(strict=must_exist)
        if resolved.parent != resolved_root:
            raise ControllerRefusal("APK path escapes private storage")
        if must_exist and (not resolved.is_file() or path.is_symlink()):
            raise ControllerRefusal("APK file is unavailable")
        return resolved

    def list_apks(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in self.root.glob("*.apk"):
            if not path.is_file() or path.is_symlink():
                continue
            metadata_path = path.with_name(f"{path.name}.json")
            data: dict[str, Any] = {}
            if metadata_path.is_file() and not metadata_path.is_symlink():
                try:
                    raw = json.loads(metadata_path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        data = raw
                except (OSError, json.JSONDecodeError):
                    pass
            package = str(data.get("package", ""))
            if not PACKAGE_PATTERN.fullmatch(package):
                package = re.sub(r"-\d+(?:-.*)?$", "", path.stem)
                if not PACKAGE_PATTERN.fullmatch(package):
                    package = "unknown"
            version, source = version_from_download_data(data, path.name)
            rows.append(
                {
                    "file": path.name,
                    "package": package,
                    "versionCode": int(data.get("versionCode", version) or 0),
                    "versionSource": str(data.get("versionSource", source)),
                    "size": path.stat().st_size,
                    "mtime": path.stat().st_mtime,
                }
            )
        return sorted(rows, key=lambda item: float(item["mtime"]), reverse=True)

    def delete(self, filename: str) -> None:
        path = self.resolve(filename)
        path.unlink()
        path.with_name(f"{path.name}.json").unlink(missing_ok=True)

    def exact_apk(self, package: str, version_code: int) -> str:
        for item in self.list_apks():
            if item["package"] == package and item["versionCode"] == version_code:
                return str(item["file"])
        return ""


JsonFetcher = Callable[[str], dict[str, Any]]


class ApkDownloader:
    def __init__(
        self,
        repository: ApkRepository,
        endpoint: str = DEFAULT_DOWNLOADER_ENDPOINT,
        *,
        fetch_json: JsonFetcher | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
    ) -> None:
        self.repository = repository
        self.endpoint = validate_downloader_endpoint(endpoint)
        self.fetch_json = fetch_json or self._fetch_json
        self.sleeper = sleeper
        self.clock = clock
        self.logger = logger or logging.getLogger(__name__)

    @staticmethod
    def _fetch_json(url: str) -> dict[str, Any]:
        request = Request(  # noqa: S310 - endpoint is validated HTTPS on an allowlisted host.
            url,
            headers={"Accept": "application/json", "User-Agent": "TV-Safety/0.1"},
        )
        with urlopen(request, timeout=20) as response:  # noqa: S310 - fixed HTTPS host.
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ControllerRefusal("Downloader response exceeds 1 MiB")
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ControllerRefusal("Downloader response is not a JSON object")
        return value

    def _poll(
        self, package: str, task_id: str, timeout: int = POLL_TIMEOUT
    ) -> dict[str, Any]:
        started = self.clock()
        attempt = 0
        last_message = "No response"
        while self.clock() - started < timeout:
            attempt += 1
            try:
                data = self.fetch_json(f"{self.endpoint}&id={quote(package)}")
                last_message = str(data.get("message") or data.get("status") or "Processing")
                download_url = str(data.get("downloadUrl", "")).replace("\\/", "/")
                if data.get("success") is True and download_url:
                    data["downloadUrl"] = download_url
                    return data
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                last_message = str(exc)
            elapsed = int(self.clock() - started)
            self.repository.tasks.update(
                task_id,
                stage="waiting",
                status="Waiting for APK link",
                attempt=attempt,
                wait_seconds=timeout,
                elapsed_seconds=elapsed,
                progress=min(17, max(1, int(elapsed / timeout * 17))),
                message=last_message,
            )
            self.sleeper(5 if attempt <= 6 else 10)
        raise TimeoutError(f"APK downloader timeout for {package}: {last_message}")

    @staticmethod
    def _validated_download_url(value: str) -> str:
        normalized = value.replace("\\/", "/")
        parsed = urlparse(normalized)
        if parsed.scheme != "https" or parsed.hostname != "online-apk-downloader.com":
            raise ControllerRefusal("Downloader returned an unapproved download URL")
        return normalized

    @staticmethod
    def _response_chunks(response: BinaryIO) -> Iterator[bytes]:
        while True:
            chunk = response.read(READ_CHUNK)
            if not chunk:
                break
            yield chunk

    def download(self, package: str, task_id: str | None = None) -> dict[str, Any]:
        package = parse_package_input(package)
        task_id = task_id or self.repository.tasks.new(package)
        part: Path | None = None
        final: Path | None = None
        try:
            data = self._poll(package, task_id)
            download_url = self._validated_download_url(str(data["downloadUrl"]))
            remote_name = Path(urlparse(download_url).path).name
            filename = safe_apk_filename(remote_name or f"{package}-{int(time.time())}.apk")
            final = self.repository.resolve(filename, must_exist=False)
            part = final.with_name(f"{final.name}.part")
            request = Request(  # noqa: S310 - download URL is validated immediately above.
                download_url, headers={"User-Agent": "TV-Safety/0.1"}
            )
            with urlopen(request, timeout=30) as response:  # noqa: S310 - URL host validated.
                length_header = response.headers.get("Content-Length")
                total = int(length_header) if length_header and length_header.isdigit() else 0
                if total > MAX_APK_BYTES:
                    raise ControllerRefusal("APK exceeds the 500 MiB download limit")
                done = 0
                with part.open("wb") as handle:
                    for chunk in self._response_chunks(response):
                        done += len(chunk)
                        if done > MAX_APK_BYTES:
                            raise ControllerRefusal("APK exceeds the 500 MiB download limit")
                        handle.write(chunk)
                        self.repository.tasks.update(
                            task_id,
                            stage="downloading",
                            status="Downloading APK",
                            bytes_done=done,
                            bytes_total=total,
                            progress=min(99, 17 + int(done / total * 82)) if total else 17,
                        )
                    handle.flush()
                    os.fsync(handle.fileno())
            os.chmod(part, 0o600)
            part.replace(final)
            inspected = inspect_apk(final)
            version, source = version_from_download_data(data, filename)
            metadata = {
                "package": package,
                "file": filename,
                "size": final.stat().st_size,
                "downloadUrl": download_url,
                "downloaderResponse": data,
                "versionCode": version,
                "versionSource": source,
                "signatureSummary": inspected.signature_summary,
                "created_at": time.time(),
            }
            atomic_write_json(final.with_name(f"{final.name}.json"), metadata)
            self.repository.tasks.update(
                task_id,
                stage="done",
                status="Downloaded",
                file=filename,
                size=metadata["size"],
                versionCode=version,
                versionSource=source,
                progress=100,
            )
            return metadata
        except Exception as exc:
            if part is not None:
                part.unlink(missing_ok=True)
            if final is not None and not final.with_name(f"{final.name}.json").exists():
                final.unlink(missing_ok=True)
            self.repository.tasks.update(
                task_id,
                stage="failed",
                status="Failed",
                error=str(exc),
                progress=100,
            )
            self.logger.exception("APK task %s failed for %s", task_id, package)
            raise

    def available_version(
        self, package: str, task_id: str
    ) -> tuple[int, str, dict[str, Any]]:
        data = self._poll(parse_package_input(package), task_id)
        version, source = version_from_download_data(data)
        return version, source, data


class AdbClient:
    """Validated ADB client for narrowly scoped third-party package operations."""

    def __init__(self, host: str, timeout: int = 45) -> None:
        self.host = validate_adb_host(host)
        self.timeout = timeout
        self.executable = shutil.which("adb") or "/usr/bin/adb"

    def _run(self, *arguments: str, timeout: int | None = None) -> str:
        try:
            result = subprocess.run(  # noqa: S603 - fixed executable and validated arguments.
                [self.executable, "-s", self.host, *arguments],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout or self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise ControllerRefusal(
                f"ADB operation timed out for {self.host}. "
                "Verify the TV connection and authorization."
            ) from exc
        output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
        if result.returncode != 0:
            raise ControllerRefusal(self._friendly_error(output))
        return output

    def _friendly_error(self, output: str) -> str:
        normalized = output.lower()
        if "unauthorized" in normalized:
            return "TV rejected ADB authorization. Confirm the RSA prompt on the TV."
        if "offline" in normalized:
            return "TV is visible but ADB is offline. Re-enable ADB on the TV and retry."
        if "cannot connect" in normalized or "failed to connect" in normalized:
            return f"TV is not accepting ADB at {self.host}. Check its address and ADB setting."
        if "daemon" in normalized or "adb server" in normalized:
            return "Controller could not start its local ADB service. Redeploy with tv.sh."
        return "ADB could not complete the request. Check the TV connection and authorization."

    def preflight(self) -> None:
        host, port_text = self.host.rsplit(":", 1)
        try:
            with socket.create_connection((host, int(port_text)), timeout=2):
                pass
        except OSError as exc:
            raise ControllerRefusal(
                f"TV is not reachable at {self.host}. "
                "Turn it on, enable ADB, and check the address."
            ) from exc
        self.connect()
        state = self._run("get-state", timeout=10).strip().lower()
        if state != "device":
            raise ControllerRefusal("TV ADB is not ready. Authorize this Controller on the TV.")

    def connect(self) -> None:
        try:
            result = subprocess.run(  # noqa: S603 - fixed executable and validated host.
                [self.executable, "connect", self.host],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except subprocess.TimeoutExpired as exc:
            raise ControllerRefusal(
                f"ADB connection timed out for {self.host}. "
                "Enable ADB and authorize this Controller."
            ) from exc
        combined = f"{result.stdout}\n{result.stderr}".lower()
        if result.returncode != 0 or any(
            marker in combined for marker in ("failed", "cannot connect", "unable to connect")
        ):
            detail = (result.stderr or result.stdout).strip()
            raise ControllerRefusal(self._friendly_error(detail))

    def installed_version(self, package: str) -> int:
        state = self.package_state(package)
        if not state.installed:
            raise ControllerRefusal(f"Installed version is unavailable for {package}")
        return state.version_code

    def _listed(self, package: str, flag: str) -> bool:
        output = self._run("shell", "pm", "list", "packages", flag, package)
        return f"package:{package}" in output.splitlines()

    def device_identity(self) -> DeviceIdentity:
        values = {
            "model": self._run("shell", "getprop", "ro.product.model").strip(),
            "platform": self._run("shell", "getprop", "ro.build.characteristics").strip(),
            "firmware": self._run("shell", "getprop", "ro.build.version.incremental").strip(),
            "fingerprint": self._run("shell", "getprop", "ro.build.fingerprint").strip(),
        }
        if not values["fingerprint"]:
            raise ControllerRefusal("TV build fingerprint is unavailable")
        return DeviceIdentity(**values)

    def package_state(self, package: str) -> PackageState:
        package = parse_package_input(package)
        output = self._run("shell", "dumpsys", "package", package)
        version_code = re.search(r"\bversionCode=(\d+)", output)
        if version_code is None:
            return PackageState(False, False, False)
        version_name = re.search(r"\bversionName=([^\s]+)", output)
        third_party = self._listed(package, "-3")
        disabled = self._listed(package, "-d")
        return PackageState(
            True,
            not disabled,
            third_party,
            int(version_code.group(1)),
            version_name.group(1) if version_name else "unknown",
        )

    def third_party_packages(self) -> list[str]:
        output = self._run("shell", "pm", "list", "packages", "-3")
        packages = [line.removeprefix("package:").strip() for line in output.splitlines()]
        return sorted(item for item in packages if PACKAGE_PATTERN.fullmatch(item))

    def set_enabled(self, package: str, enabled: bool) -> str:
        package = parse_package_input(package)
        if is_never_touch(package):
            raise ControllerRefusal("Package controls target a never-touch package")
        state = self.package_state(package)
        if not state.installed or not state.third_party:
            raise ControllerRefusal(
                "Only an installed third-party package can be enabled or disabled"
            )
        action = "enable" if enabled else "disable-user"
        return self._run("shell", "pm", action, "--user", "0", package)

    def install(self, apk: Path, package: str) -> str:
        package = parse_package_input(package)
        if is_never_touch(package):
            raise ControllerRefusal("Install/update targets a never-touch package")
        self.connect()
        if self._listed(package, "-s"):
            raise ControllerRefusal("Install/update of a system package is prohibited")
        return self._run("install", "-r", str(apk), timeout=300)

    def uninstall(self, package: str) -> str:
        package = parse_package_input(package)
        if is_never_touch(package):
            raise ControllerRefusal("Uninstall targets a never-touch package")
        self.connect()
        if not self._listed(package, "-3"):
            raise ControllerRefusal("Only verified third-party packages can be uninstalled")
        return self._run("uninstall", package)


class ApkJobManager:
    """Coordinate preflighted APK tasks, journaling, rollback, and state profiles."""

    def __init__(
        self,
        repository: ApkRepository,
        settings: ApkSettingsStore,
        *,
        downloader_factory: Callable[[str], ApkDownloader] | None = None,
        adb_factory: Callable[[str], AdbClient] = AdbClient,
        journal: StateJournal | None = None,
        profiles: StateProfileStore | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.downloader_factory = downloader_factory or (
            lambda endpoint: ApkDownloader(repository, endpoint)
        )
        self.adb_factory = adb_factory
        self.journal = journal or StateJournal(repository.root.parent / "operations.sqlite3")
        self.profiles = profiles or StateProfileStore(repository.root.parent / "profiles")
        self._lock = threading.Lock()
        self._running: set[str] = set()

    def _start(self, task_id: str, target: Callable[[], None]) -> None:
        def run() -> None:
            try:
                target()
            except Exception:
                logging.getLogger(__name__).exception("Background APK task %s failed", task_id)
            finally:
                with self._lock:
                    self._running.discard(task_id)

        with self._lock:
            self._running.add(task_id)
        threading.Thread(target=run, name=f"apk-{task_id}", daemon=True).start()

    def start_download(self, package: str) -> str:
        package = parse_package_input(package)
        task_id = self.repository.tasks.new(package)
        endpoint = self.settings.load().downloader_endpoint
        def download() -> None:
            self.downloader_factory(endpoint).download(package, task_id)

        self._start(task_id, download)
        return task_id

    def _require_sideload(self) -> ApkSettings:
        settings = self.settings.load()
        if not settings.sideload_enabled:
            raise ControllerRefusal("Enable sideloading in Controller Settings first")
        return settings

    def check_tv(self, settings: ApkSettings | None = None) -> AdbClient:
        resolved = settings or self.settings.load()
        client = self.adb_factory(resolved.adb_host)
        client.preflight()
        return client

    def _mutation_context(
        self, adb: AdbClient, package: str, *, require_archive: bool
    ) -> tuple[DeviceIdentity, PackageState, str]:
        identity = adb.device_identity()
        before = adb.package_state(package)
        before_apk = (
            self.repository.exact_apk(package, before.version_code) if before.installed else ""
        )
        if require_archive and before.installed and not before_apk:
            raise ControllerRefusal(
                "Exact pre-change APK is not archived; this operation would not be reversible"
            )
        return identity, before, before_apk

    @staticmethod
    def _verify_changed_state(package: str, state: PackageState) -> None:
        if not state.installed or not state.third_party:
            raise ControllerRefusal(f"Post-change state is not a third-party package: {package}")

    def start_install(self, filename: str) -> str:
        settings = self._require_sideload()
        apk = self.repository.resolve(filename)
        metadata = next(
            (item for item in self.repository.list_apks() if item["file"] == apk.name), None
        )
        if metadata is None or metadata["package"] == "unknown":
            raise ControllerRefusal("APK package metadata is unavailable")
        package = parse_package_input(str(metadata["package"]))
        if is_never_touch(package):
            raise ControllerRefusal("Install/update targets a never-touch package")
        adb = self.check_tv(settings)
        identity, before, before_apk = self._mutation_context(
            adb, package, require_archive=True
        )
        operation_id = self.journal.begin(
            package=package,
            action="update" if before.installed else "install",
            before=before,
            device=identity,
            before_apk=before_apk,
            after_apk=apk.name,
            inverse_action="restore-apk" if before.installed else "uninstall",
        )
        task_id = self.repository.tasks.new(package, kind="install")

        def install() -> None:
            try:
                self.repository.tasks.update(
                    task_id, stage="installing", status="Installing on TV", progress=40
                )
                output = adb.install(apk, package)
                after = adb.package_state(package)
                self._verify_changed_state(package, after)
                self.journal.complete(
                    operation_id,
                    after,
                    "restore-apk" if before.installed else "uninstall",
                )
                self.repository.tasks.update(
                    task_id, stage="result", status="Installed", detail=output, progress=100
                )
            except Exception as exc:
                self.journal.fail(operation_id, str(exc))
                self.repository.tasks.update(
                    task_id, stage="failed", status="Failed", error=str(exc), progress=100
                )
                raise

        self._start(task_id, install)
        return task_id

    def start_uninstall(self, package: str) -> str:
        settings = self._require_sideload()
        package = parse_package_input(package)
        if is_never_touch(package):
            raise ControllerRefusal("Uninstall targets a never-touch package")
        adb = self.check_tv(settings)
        identity, before, before_apk = self._mutation_context(
            adb, package, require_archive=True
        )
        if not before.installed or not before.third_party:
            raise ControllerRefusal("Only an installed third-party package can be uninstalled")
        operation_id = self.journal.begin(
            package=package,
            action="uninstall",
            before=before,
            device=identity,
            before_apk=before_apk,
            inverse_action="install-apk",
        )
        task_id = self.repository.tasks.new(package, kind="uninstall")

        def uninstall() -> None:
            try:
                self.repository.tasks.update(
                    task_id, stage="uninstalling", status="Uninstalling from TV", progress=40
                )
                output = adb.uninstall(package)
                after = adb.package_state(package)
                if after.installed:
                    raise ControllerRefusal("Package is still installed after uninstall")
                self.journal.complete(operation_id, after, "install-apk")
                self.repository.tasks.update(
                    task_id, stage="result", status="Uninstalled", detail=output, progress=100
                )
            except Exception as exc:
                self.journal.fail(operation_id, str(exc))
                self.repository.tasks.update(
                    task_id, stage="failed", status="Failed", error=str(exc), progress=100
                )
                raise

        self._start(task_id, uninstall)
        return task_id

    def start_check_update(self, package: str) -> str:
        settings = self.settings.load()
        package = parse_package_input(package)
        adb = self.check_tv(settings)
        task_id = self.repository.tasks.new(package, kind="check-update")

        def check() -> None:
            try:
                self.repository.tasks.update(
                    task_id, stage="checking", status="Checking installed version", progress=20
                )
                installed = adb.installed_version(package)
                available, source, data = self.downloader_factory(
                    settings.downloader_endpoint
                ).available_version(package, task_id)
                self.repository.tasks.update(
                    task_id,
                    stage="result",
                    status=("Update available" if available > installed else "No update detected"),
                    installedVersionCode=installed,
                    availableVersionCode=available,
                    availableVersionSource=source,
                    updateAvailable=available > installed,
                    downloadUrl=data.get("downloadUrl", ""),
                    progress=100,
                )
            except Exception as exc:
                self.repository.tasks.update(
                    task_id, stage="failed", status="Failed", error=str(exc), progress=100
                )
                raise

        self._start(task_id, check)
        return task_id

    def start_update(self, package: str) -> str:
        settings = self._require_sideload()
        package = parse_package_input(package)
        if is_never_touch(package):
            raise ControllerRefusal("Install/update targets a never-touch package")
        adb = self.check_tv(settings)
        identity, before, before_apk = self._mutation_context(
            adb, package, require_archive=True
        )
        if not before.installed or not before.third_party:
            raise ControllerRefusal("Only an installed third-party package can be updated")
        task_id = self.repository.tasks.new(package, kind="update")

        def update() -> None:
            try:
                metadata = self.downloader_factory(settings.downloader_endpoint).download(
                    package, task_id
                )
                apk = self.repository.resolve(str(metadata["file"]))
                operation_id = self.journal.begin(
                    package=package,
                    action="update",
                    before=before,
                    device=identity,
                    before_apk=before_apk,
                    after_apk=apk.name,
                    inverse_action="restore-apk",
                )
                self.repository.tasks.update(
                    task_id, stage="installing", status="Installing update on TV", progress=99
                )
                output = adb.install(apk, package)
                after = adb.package_state(package)
                self._verify_changed_state(package, after)
                self.journal.complete(operation_id, after, "restore-apk")
                self.repository.tasks.update(
                    task_id, stage="result", status="Updated", detail=output, progress=100
                )
            except Exception as exc:
                if "operation_id" in locals():
                    self.journal.fail(operation_id, str(exc))
                self.repository.tasks.update(
                    task_id, stage="failed", status="Failed", error=str(exc), progress=100
                )
                raise

        self._start(task_id, update)
        return task_id

    def start_set_enabled(self, package: str, *, enabled: bool) -> str:
        settings = self._require_sideload()
        package = parse_package_input(package)
        if is_never_touch(package):
            raise ControllerRefusal("Package controls target a never-touch package")
        adb = self.check_tv(settings)
        identity, before, before_apk = self._mutation_context(
            adb, package, require_archive=False
        )
        if not before.installed or not before.third_party:
            raise ControllerRefusal("Only an installed third-party package can be controlled")
        if before.enabled is enabled:
            raise ControllerRefusal("Package already has the requested enabled state")
        action = "enable" if enabled else "disable"
        operation_id = self.journal.begin(
            package=package,
            action=action,
            before=before,
            device=identity,
            before_apk=before_apk,
            inverse_action="disable" if enabled else "enable",
        )
        task_id = self.repository.tasks.new(package, kind=action)

        def change_state() -> None:
            try:
                self.repository.tasks.update(
                    task_id,
                    stage="applying",
                    status=f"{action.title()} user package",
                    progress=50,
                )
                output = adb.set_enabled(package, enabled)
                after = adb.package_state(package)
                if after.enabled is not enabled:
                    raise ControllerRefusal("Android did not apply the requested enabled state")
                self.journal.complete(operation_id, after, "disable" if enabled else "enable")
                self.repository.tasks.update(
                    task_id,
                    stage="result",
                    status=f"Package {action}d",
                    detail=output,
                    progress=100,
                )
            except Exception as exc:
                self.journal.fail(operation_id, str(exc))
                self.repository.tasks.update(
                    task_id, stage="failed", status="Failed", error=str(exc), progress=100
                )
                raise

        self._start(task_id, change_state)
        return task_id

    def start_rollback(self, operation_id: int) -> str:
        settings = self._require_sideload()
        adb = self.check_tv(settings)
        original = self.journal.get(operation_id)
        current = adb.package_state(original.package)
        device = adb.device_identity()
        original = self.journal.prepare_selective_rollback(operation_id, current, device)
        if original.status == "uncertain" and current == original.before:
            self.journal.reconcile_no_change(original.id)
            task_id = self.repository.tasks.new(original.package, kind="reconcile")
            self.repository.tasks.update(
                task_id,
                stage="result",
                status="No rollback needed",
                detail="Live TV state already matches the recorded before-state",
                progress=100,
            )
            return task_id
        rollback_id = self.journal.begin(
            package=original.package,
            action=f"rollback:{original.action}",
            before=current,
            device=device,
            before_apk=original.after_apk,
            after_apk=original.before_apk,
            parent_id=original.id,
        )
        task_id = self.repository.tasks.new(original.package, kind="rollback")

        def rollback() -> None:
            try:
                self.repository.tasks.update(
                    task_id, stage="rolling-back", status="Restoring recorded state", progress=50
                )
                self._apply_inverse(adb, original)
                after = adb.package_state(original.package)
                if after != original.before:
                    raise ControllerRefusal(
                        "Rollback result does not match the recorded before-state"
                    )
                self.journal.complete(rollback_id, after, "")
                self.journal.finish_rollback(original.id, rollback_id)
                self.repository.tasks.update(
                    task_id, stage="result", status="Selected operation reverted", progress=100
                )
            except Exception as exc:
                self.journal.fail(rollback_id, str(exc))
                self.repository.tasks.update(
                    task_id, stage="failed", status="Rollback failed", error=str(exc), progress=100
                )
                raise

        self._start(task_id, rollback)
        return task_id

    def _apply_inverse(self, adb: AdbClient, original: OperationRecord) -> None:
        if original.inverse_action == "uninstall":
            adb.uninstall(original.package)
        elif original.inverse_action in {"install-apk", "restore-apk"}:
            apk = self.repository.resolve(original.before_apk)
            adb.install(apk, original.package)
            if not original.before.enabled:
                adb.set_enabled(original.package, False)
        elif original.inverse_action == "enable":
            adb.set_enabled(original.package, True)
        elif original.inverse_action == "disable":
            adb.set_enabled(original.package, False)
        else:
            raise ControllerRefusal("operation has no supported inverse")

    def start_profile_capture(self, name: str) -> str:
        name = self.profiles.validate_name(name)
        adb = self.check_tv()
        device = adb.device_identity()
        task_id = self.repository.tasks.new("profile.capture", kind="profile-capture")

        def capture() -> None:
            try:
                packages = adb.third_party_packages()
                rows: list[ProfilePackage] = []
                for index, package in enumerate(packages, start=1):
                    if is_never_touch(package):
                        continue
                    state = adb.package_state(package)
                    apk_file = self.repository.exact_apk(package, state.version_code)
                    if not apk_file:
                        try:
                            self.downloader_factory(
                                self.settings.load().downloader_endpoint
                            ).download(package)
                            apk_file = self.repository.exact_apk(
                                package, state.version_code
                            )
                        except Exception:
                            logging.getLogger(__name__).warning(
                                "Exact APK could not be archived for profile package %s",
                                package,
                            )
                    rows.append(
                        ProfilePackage(
                            package,
                            state.enabled,
                            state.version_code,
                            state.version_name,
                            apk_file,
                        )
                    )
                    self.repository.tasks.update(
                        task_id,
                        stage="capturing",
                        status="Capturing user application state",
                        progress=int(index / max(1, len(packages)) * 95),
                    )
                profile = self.profiles.save(name, device, rows)
                self.repository.tasks.update(
                    task_id,
                    stage="result",
                    status="State profile saved",
                    detail=(
                        f"{profile.name} · {len(profile.packages)} packages · "
                        f"{'restore ready' if profile.complete else 'missing exact APK archives'}"
                    ),
                    progress=100,
                )
            except Exception as exc:
                self.repository.tasks.update(
                    task_id, stage="failed", status="Profile capture failed", error=str(exc),
                    progress=100
                )
                raise

        self._start(task_id, capture)
        return task_id

    def profile_preview(self, profile_id: str) -> dict[str, Any]:
        profile = self.profiles.get(profile_id)
        adb = self.check_tv()
        device = adb.device_identity()
        if device != profile.device:
            raise ControllerRefusal(
                "profile model, firmware, or fingerprint does not match this TV"
            )
        operations: list[dict[str, str]] = []
        blockers: list[str] = []
        for desired in profile.packages:
            if is_never_touch(desired.package):
                blockers.append(f"{desired.package}: never-touch package is prohibited")
                continue
            current = adb.package_state(desired.package)
            if not desired.apk_file:
                blockers.append(f"{desired.package}: exact APK archive is missing")
                continue
            if not current.installed:
                operations.append({"package": desired.package, "action": "install"})
            elif current.version_code != desired.version_code:
                if not self.repository.exact_apk(desired.package, current.version_code):
                    blockers.append(
                        f"{desired.package}: current version APK is missing, "
                        "so rollback is impossible"
                    )
                    continue
                operations.append({"package": desired.package, "action": "restore-version"})
            if current.installed and current.enabled != desired.enabled:
                operations.append(
                    {
                        "package": desired.package,
                        "action": "enable" if desired.enabled else "disable",
                    }
                )
        return {
            "profile": profile,
            "operations": operations,
            "blockers": blockers,
            "ready": not blockers,
        }

    def start_profile_apply(self, profile_id: str) -> str:
        self._require_sideload()
        preview = self.profile_preview(profile_id)
        profile = preview["profile"]
        if not isinstance(profile, StateProfile):
            raise ControllerRefusal("profile preview is invalid")
        blockers = preview["blockers"]
        if blockers:
            raise ControllerRefusal(str(blockers[0]))
        adb = self.check_tv()
        task_id = self.repository.tasks.new("profile.restore", kind="profile-apply")

        def apply_profile() -> None:
            try:
                total = max(1, len(profile.packages))
                for index, desired in enumerate(profile.packages, start=1):
                    self._apply_profile_package(adb, profile, desired)
                    self.repository.tasks.update(
                        task_id,
                        stage="applying-profile",
                        status="Applying exact state profile",
                        detail=f"{index} / {len(profile.packages)} · {desired.package}",
                        progress=int(index / total * 99),
                    )
                self.repository.tasks.update(
                    task_id,
                    stage="result",
                    status="State profile applied",
                    detail=profile.name,
                    progress=100,
                )
            except Exception as exc:
                self.repository.tasks.update(
                    task_id,
                    stage="failed",
                    status="Profile apply stopped",
                    error=str(exc),
                    progress=100,
                )
                raise

        self._start(task_id, apply_profile)
        return task_id

    def _apply_profile_package(
        self, adb: AdbClient, profile: StateProfile, desired: ProfilePackage
    ) -> None:
        current = adb.package_state(desired.package)
        if not current.installed or current.version_code != desired.version_code:
            before_apk = (
                self.repository.exact_apk(desired.package, current.version_code)
                if current.installed
                else ""
            )
            if current.installed and not before_apk:
                raise ControllerRefusal(
                    f"{desired.package}: current exact APK disappeared; profile apply stopped"
                )
            operation_id = self.journal.begin(
                package=desired.package,
                action="profile-update" if current.installed else "profile-install",
                before=current,
                device=profile.device,
                before_apk=before_apk,
                after_apk=desired.apk_file,
                batch_id=profile.id,
                inverse_action="restore-apk" if current.installed else "uninstall",
            )
            try:
                adb.install(self.repository.resolve(desired.apk_file), desired.package)
                after = adb.package_state(desired.package)
                if not after.installed or after.version_code != desired.version_code:
                    raise ControllerRefusal(
                        f"{desired.package}: installed version does not match the profile"
                    )
                self.journal.complete(
                    operation_id,
                    after,
                    "restore-apk" if current.installed else "uninstall",
                )
                current = after
            except Exception as exc:
                self.journal.fail(operation_id, str(exc))
                raise
        if current.enabled != desired.enabled:
            action = "profile-enable" if desired.enabled else "profile-disable"
            operation_id = self.journal.begin(
                package=desired.package,
                action=action,
                before=current,
                device=profile.device,
                before_apk=desired.apk_file,
                batch_id=profile.id,
                inverse_action="disable" if desired.enabled else "enable",
            )
            try:
                adb.set_enabled(desired.package, desired.enabled)
                after = adb.package_state(desired.package)
                if after.enabled != desired.enabled:
                    raise ControllerRefusal(
                        f"{desired.package}: enabled state does not match the profile"
                    )
                self.journal.complete(
                    operation_id, after, "disable" if desired.enabled else "enable"
                )
            except Exception as exc:
                self.journal.fail(operation_id, str(exc))
                raise

    def live(self) -> dict[str, Any]:
        tasks = self.repository.tasks.active()
        for task in tasks:
            stage = str(task.get("stage", ""))
            if stage == "waiting":
                elapsed = int(task.get("elapsed_seconds", 0))
                limit = int(task.get("wait_seconds", POLL_TIMEOUT))
                task["detail"] = f"waiting {elapsed}s / {limit}s | {max(0, limit - elapsed)}s left"
            elif stage == "downloading":
                done = int(task.get("bytes_done", 0))
                total = int(task.get("bytes_total", 0))
                task["detail"] = f"{done} / {total} bytes" if total else f"{done} bytes"
            elif stage == "result" and task.get("kind") == "check-update":
                task["detail"] = (
                    f"Installed version code {task.get('installedVersionCode', 0)} | "
                    f"available version code {task.get('availableVersionCode', 0)} | "
                    f"source: {task.get('availableVersionSource', 'unknown')}"
                )
            elif task.get("error"):
                task["detail"] = task_error_message(task["error"])
        return {
            "apk_tasks": tasks,
            "apk_table": self.repository.list_apks(),
            "sideload_enabled": self.settings.load().sideload_enabled,
            "adb_host": self.settings.load().adb_host,
        }

    def clear_finished(self) -> int:
        return self.repository.tasks.clear_finished()

    def delete_local_apk(self, filename: str) -> None:
        for operation in self.journal.list(limit=10000):
            if operation.status in {"applied", "uncertain"} and filename in {
                operation.before_apk,
                operation.after_apk,
            }:
                raise ControllerRefusal(
                    f"APK is retained by rollback operation #{operation.id}"
                )
        for profile in self.profiles.list():
            if any(item.apk_file == filename for item in profile.packages):
                raise ControllerRefusal(f"APK is retained by state profile: {profile.name}")
        self.repository.delete(filename)
