# TV Controller

TV Controller is the guarded maintenance service in the
[TV Safety suite](../README.md). The root README is the canonical installation, compatibility, ADB,
operations, and troubleshooting guide.

Controller runs on port `8091`, shares the Observer password, consumes checksum-verified Observer
snapshots, and stores its private state under `/srv/tv-safety-data/controller`.

## Responsibilities

- Maintain a private APK library.
- Check available application versions without changing the TV.
- Install, update, uninstall, enable, or disable verified third-party packages after confirmation.
- Reject system packages and built-in never-touch package families.
- Capture exact before/after state in a durable SQLite operation journal.
- Selectively revert an operation while retaining unrelated later operations.
- Capture named user-application profiles and apply them only to the same exact TV build.
- Retain APK files required by active rollback records or saved profiles.

System-package behavior remains inspect, exact-profile verification, planning, and dry-run only.

## Safety Invariants

- Sideload mutation is disabled by default.
- Every mutation requires live ADB preflight and explicit confirmation.
- ADB arguments are validated and executed without a shell.
- An exact prior APK is required before a destructive or version-replacing operation.
- A newer unresolved operation on the same package blocks an older rollback branch.
- Device model, firmware, and fingerprint drift blocks profile application and rollback.
- Profile application is sequential and stops on the first mismatch.
- Automatic mutation retries are prohibited.

## Local Development

From the repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ./tv-shared
.venv/bin/pip install -r tv-controller/requirements-dev.txt
.venv/bin/pip install -e ./tv-controller
.venv/bin/pytest tv-controller/tests
.venv/bin/ruff check tv-controller/src tv-controller/tests
PYTHONPATH=tv-shared/src:tv-controller/src .venv/bin/mypy tv-controller/src
```

Useful command-line entry points:

```bash
tv-controller inspect SNAPSHOT
tv-controller profile verify PROFILE SNAPSHOT
tv-controller dry-run PROFILE SNAPSHOT \
  --connection-verified --authorization-verified
tv-apk --help
```

See [Architecture](docs/ARCHITECTURE.md), [Security](docs/SECURITY.md),
[Recovery](docs/RECOVERY.md), and [Threat Model](docs/THREAT_MODEL.md) for implementation details.
