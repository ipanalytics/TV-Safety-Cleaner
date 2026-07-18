from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from tv_controller.apk import archive_apk, inspect_apk
from tv_controller.planner import build_dry_run
from tv_controller.profile import load_profile, verify_profile
from tv_controller.snapshot import ControllerRefusal, load_snapshot
from tv_controller.transactions import OperationJournal, restore_dry_run, rollback_dry_run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tv-controller",
        description="Dry-run-first TV controller",
    )
    parser.add_argument("--version", action="version", version="tv-controller 0.1.0")
    commands = parser.add_subparsers(dest="command")
    inspect = commands.add_parser("inspect", help="inspect a checksum-valid Observer snapshot")
    inspect.add_argument("snapshot", type=Path)
    profile = commands.add_parser("profile", help="verify exact device profile compatibility")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    profile_verify = profile_commands.add_parser("verify")
    profile_verify.add_argument("profile", type=Path)
    profile_verify.add_argument("snapshot", type=Path)
    for name in ("plan", "dry-run"):
        planner = commands.add_parser(name, help="produce a non-mutating exact-profile plan")
        planner.add_argument("profile", type=Path)
        planner.add_argument("snapshot", type=Path)
        planner.add_argument("--connection-verified", action="store_true")
        planner.add_argument("--authorization-verified", action="store_true")
    rollback = commands.add_parser("rollback", help="show Controller-recorded rollback operations")
    rollback.add_argument("--database", type=Path, required=True)
    rollback.add_argument("--dry-run", action="store_true", required=True)
    restore = commands.add_parser("restore", help="show Controller journal restore information")
    restore.add_argument("--database", type=Path, required=True)
    restore.add_argument("--dry-run", action="store_true", required=True)
    apk = commands.add_parser("apk", help="inspect or archive a local user APK")
    apk_commands = apk.add_subparsers(dest="apk_command", required=True)
    apk_inspect = apk_commands.add_parser("inspect")
    apk_inspect.add_argument("path", type=Path)
    apk_archive = apk_commands.add_parser("archive")
    apk_archive.add_argument("path", type=Path)
    apk_archive.add_argument("--root", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inspect":
            value = load_snapshot(args.snapshot)
            print(json.dumps(value.manifest, indent=2))  # noqa: T201 - CLI output
        elif args.command == "profile" and args.profile_command == "verify":
            snapshot = load_snapshot(args.snapshot)
            profile = load_profile(args.profile)
            verify_profile(profile, snapshot)
            print(f"profile verified: {profile.profile_id}")  # noqa: T201 - CLI output
        elif args.command in {"plan", "dry-run"}:
            snapshot = load_snapshot(args.snapshot)
            profile = load_profile(args.profile)
            plan = build_dry_run(
                profile,
                snapshot,
                connection_verified=args.connection_verified,
                authorization_verified=args.authorization_verified,
            )
            print(json.dumps(asdict(plan), indent=2))  # noqa: T201 - CLI output
        elif args.command == "rollback":
            print(json.dumps(rollback_dry_run(OperationJournal(args.database)), indent=2))  # noqa: T201
        elif args.command == "restore":
            print(json.dumps(restore_dry_run(OperationJournal(args.database)), indent=2))  # noqa: T201
        elif args.command == "apk" and args.apk_command == "inspect":
            print(json.dumps(asdict(inspect_apk(args.path)), indent=2))  # noqa: T201
        elif args.command == "apk" and args.apk_command == "archive":
            print(archive_apk(args.path, args.root))  # noqa: T201 - CLI output
        else:
            build_parser().print_help()
        return 0
    except (OSError, ValueError, ControllerRefusal) as exc:
        print(f"refused: {exc}")  # noqa: T201 - CLI error
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
