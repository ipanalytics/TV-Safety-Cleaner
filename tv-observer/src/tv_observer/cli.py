from __future__ import annotations

import argparse
import json
from pathlib import Path

from tv_observer.adb import AdbError, ReadOnlyAdb
from tv_observer.collector import collect_snapshot_payload
from tv_observer.observation import ObservationError, ObservationPoller, ObservationStore
from tv_observer.recovery import default_readiness_report
from tv_observer.snapshot import (
    SnapshotError,
    SnapshotPayload,
    create_snapshot,
    inspect_snapshot,
    list_snapshots,
    verify_snapshot,
)
from tv_observer.utilities import archive_snapshot, diff_snapshots, format_diff, redact_snapshot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tv-observer", description="Read-only Smart TV observer")
    parser.add_argument("--version", action="version", version="tv-observer 0.1.0")
    commands = parser.add_subparsers(dest="command")
    snapshot = commands.add_parser("snapshot", help="create an inventory snapshot")
    snapshot.add_argument("--root", type=Path, required=True)
    snapshot.add_argument("--device", required=True)
    snapshot.add_argument("--fixture", type=Path)
    snapshot.add_argument("--serial")

    listing = commands.add_parser("list", help="list verified snapshots")
    listing.add_argument("--root", type=Path, required=True)

    inspect = commands.add_parser("inspect", help="inspect a verified snapshot")
    inspect.add_argument("path", type=Path)

    verify = commands.add_parser("verify", help="verify snapshot checksums")
    verify.add_argument("path", type=Path)

    difference = commands.add_parser("diff", help="compare two verified snapshots")
    difference.add_argument("before", type=Path)
    difference.add_argument("after", type=Path)
    difference.add_argument("--json", action="store_true", dest="as_json")

    archive = commands.add_parser("archive", help="package a verified snapshot")
    archive.add_argument("path", type=Path)
    archive.add_argument("--output", type=Path)

    redact = commands.add_parser("redact", help="create a share-safe snapshot copy")
    redact.add_argument("path", type=Path)
    redact.add_argument("--output", type=Path)

    observe = commands.add_parser("observe", help="manage lightweight observation sessions")
    observe_commands = observe.add_subparsers(dest="observe_command", required=True)
    for name in ("start", "status", "stop", "report", "run"):
        item = observe_commands.add_parser(name)
        item.add_argument("--database", type=Path, required=True)
        item.add_argument("--poll-interval", type=int, default=30)
        if name == "start":
            item.add_argument("--name", required=True)
        if name == "run":
            item.add_argument("--serial", required=True)

    scenario = commands.add_parser("scenario", help="manage observation scenario labels")
    scenario_commands = scenario.add_subparsers(dest="scenario_command", required=True)
    scenario_start = scenario_commands.add_parser("start")
    scenario_start.add_argument("--database", type=Path, required=True)
    scenario_start.add_argument("--label", required=True)
    scenario_stop = scenario_commands.add_parser("stop")
    scenario_stop.add_argument("--database", type=Path, required=True)

    recovery = commands.add_parser("recovery", help="create recovery readiness reports")
    recovery_commands = recovery.add_subparsers(dest="recovery_command", required=True)
    recovery_report = recovery_commands.add_parser("report")
    recovery_report.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "snapshot":
            if args.fixture:
                data = json.loads(args.fixture.read_text(encoding="utf-8"))
                payload = SnapshotPayload(**data)
            else:
                payload = collect_snapshot_payload(args.serial)
            output = create_snapshot(args.root, args.device, payload)
            print(output)  # noqa: T201 - CLI output
        elif args.command == "list":
            for path in list_snapshots(args.root):
                print(path)  # noqa: T201 - CLI output
        elif args.command == "inspect":
            print(json.dumps(inspect_snapshot(args.path), indent=2))  # noqa: T201
        elif args.command == "verify":
            verify_snapshot(args.path)
            print("snapshot verified")  # noqa: T201 - CLI output
        elif args.command == "diff":
            value = diff_snapshots(args.before, args.after)
            print(json.dumps(value, indent=2) if args.as_json else format_diff(value))  # noqa: T201
        elif args.command == "archive":
            print(archive_snapshot(args.path, args.output))  # noqa: T201 - CLI output
        elif args.command == "redact":
            print(redact_snapshot(args.path, args.output))  # noqa: T201 - CLI output
        elif args.command == "observe":
            store = ObservationStore(args.database, args.poll_interval)
            if args.observe_command == "start":
                print(f"observation session {store.start(args.name)} started")  # noqa: T201
            elif args.observe_command == "status":
                print(json.dumps(store.status(), indent=2))  # noqa: T201
            elif args.observe_command == "stop":
                store.stop()
                print("observation stopped")  # noqa: T201
            elif args.observe_command == "report":
                print(json.dumps(store.report(), indent=2))  # noqa: T201
            elif args.observe_command == "run":
                count = ObservationPoller(store, ReadOnlyAdb(serial=args.serial)).run()
                print(f"observation polling stopped after {count} samples")  # noqa: T201
        elif args.command == "scenario":
            store = ObservationStore(args.database)
            if args.scenario_command == "start":
                print(f"scenario {store.scenario_start(args.label)} started")  # noqa: T201
            else:
                store.scenario_stop()
                print("scenario stopped")  # noqa: T201
        elif args.command == "recovery" and args.recovery_command == "report":
            report = json.dumps(default_readiness_report(), indent=2) + "\n"
            if args.output:
                args.output.write_text(report, encoding="utf-8")
                print(args.output)  # noqa: T201 - CLI output
            else:
                print(report, end="")  # noqa: T201 - CLI output
        else:
            build_parser().print_help()
        return 0
    except (
        OSError,
        ValueError,
        AdbError,
        ObservationError,
        SnapshotError,
        json.JSONDecodeError,
    ) as exc:
        print(f"error: {exc}")  # noqa: T201 - CLI error
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
