from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from tv_controller.apk_manager import (
    AdbClient,
    ApkDownloader,
    ApkRepository,
    parse_package_input,
)
from tv_controller.snapshot import ControllerRefusal


def _root() -> Path:
    return Path(os.environ.get("TV_CONTROLLER_APK_ROOT", "/srv/tv-safety-data/controller/apks"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tv-apk", description="Private TV APK manager")
    parser.add_argument("--root", type=Path, default=_root())
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    download = commands.add_parser("download")
    download.add_argument("package")
    install = commands.add_parser("install")
    install.add_argument("apk_name")
    install.add_argument("--host", required=True)
    delete = commands.add_parser("delete")
    delete.add_argument("apk_name")
    uninstall = commands.add_parser("uninstall")
    uninstall.add_argument("package")
    uninstall.add_argument("--host", required=True)
    check = commands.add_parser("check-update")
    check.add_argument("package")
    check.add_argument("--host", required=True)
    update = commands.add_parser("update")
    update.add_argument("package")
    update.add_argument("--host", required=True)
    return parser


def _metadata(repository: ApkRepository, filename: str) -> dict[str, Any]:
    match = next((item for item in repository.list_apks() if item["file"] == filename), None)
    if match is None or match["package"] == "unknown":
        raise ControllerRefusal("APK package metadata is unavailable")
    return match


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repository = ApkRepository(args.root)
    try:
        if args.command == "list":
            result: Any = repository.list_apks()
        elif args.command == "download":
            package = parse_package_input(args.package)
            result = ApkDownloader(repository).download(package)
        elif args.command == "install":
            apk = repository.resolve(args.apk_name)
            metadata = _metadata(repository, apk.name)
            output = AdbClient(args.host).install(apk, str(metadata["package"]))
            result = {"status": "installed", "file": apk.name, "output": output}
        elif args.command == "delete":
            repository.delete(args.apk_name)
            result = {"status": "deleted", "file": args.apk_name, "tv_changed": False}
        elif args.command == "uninstall":
            package = parse_package_input(args.package)
            output = AdbClient(args.host).uninstall(package)
            result = {"status": "uninstalled", "package": package, "output": output}
        elif args.command == "check-update":
            package = parse_package_input(args.package)
            task_id = repository.tasks.new(package, kind="check-update")
            installed = AdbClient(args.host).installed_version(package)
            available, source, data = ApkDownloader(repository).available_version(
                package, task_id
            )
            result = {
                "package": package,
                "host": args.host,
                "installedVersionCode": installed,
                "availableVersionCode": available,
                "availableVersionSource": source,
                "updateAvailable": available > installed,
                "downloadUrl": data.get("downloadUrl", ""),
            }
            repository.tasks.update(
                task_id,
                stage="done",
                status="Update check complete",
                progress=100,
            )
        elif args.command == "update":
            package = parse_package_input(args.package)
            metadata = ApkDownloader(repository).download(package)
            apk = repository.resolve(str(metadata["file"]))
            output = AdbClient(args.host).install(apk, package)
            result = {
                "status": "updated",
                "package": package,
                "file": apk.name,
                "output": output,
            }
        else:
            raise ControllerRefusal("Unsupported command")
        print(json.dumps(result, ensure_ascii=True, indent=2))  # noqa: T201
        return 0
    except (OSError, ValueError, ControllerRefusal) as exc:
        print(json.dumps({"status": "refused", "error": str(exc)}))  # noqa: T201
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
