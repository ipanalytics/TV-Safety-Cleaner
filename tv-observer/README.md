# TV Observer

TV Observer is the read-only evidence service in the
[TV Safety suite](../README.md). The root README is the canonical installation, compatibility, ADB,
first-run, and troubleshooting guide.

Observer runs on port `8090`, owns the canonical TV inventory, and stores private snapshots under
`/srv/tv-safety-data/observer`.

## Responsibilities

- Discover ADB devices and expose authorization state.
- Execute only a fixed allowlist of inventory reads.
- Detect Android TV, Google TV, Fire OS, Vega signals, or an unknown/conflicting platform.
- Inventory user, system, updated-system, disabled, and unknown packages.
- Capture package versions, enabled state, requested permissions, firmware, launcher, memory,
  storage, process, and network evidence.
- Write atomic, SHA-256-protected snapshots.
- Export application inventory as text or JSON.
- Diff, redact, verify, and archive snapshots without changing the television.

Observer never installs, removes, enables, disables, stops, clears, reboots, resets, or writes TV
settings.

## Read Allowlist

The ADB adapter exposes fixed methods for devices, properties, package lists, package summaries,
activity, memory, storage, process, network, CPU, and selected setting reads. There is no web or CLI
endpoint for arbitrary shell commands.

## Local Development

From the repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ./tv-shared
.venv/bin/pip install -r tv-observer/requirements-dev.txt
.venv/bin/pip install -e ./tv-observer
.venv/bin/pytest tv-observer/tests
.venv/bin/ruff check tv-observer/src tv-observer/tests
PYTHONPATH=tv-shared/src:tv-observer/src .venv/bin/mypy tv-observer/src
```

Useful command-line entry points:

```bash
tv-observer --help
tv-observer snapshot --root PRIVATE_ROOT --device TV_NAME --serial ADB_SERIAL
tv-observer verify SNAPSHOT_PATH
```

See [Architecture](docs/ARCHITECTURE.md), [Security](docs/SECURITY.md),
[Privacy](docs/PRIVACY.md), and [Snapshot Format](docs/SNAPSHOT_FORMAT.md) for implementation
details.
