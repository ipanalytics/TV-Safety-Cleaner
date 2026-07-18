# Architecture

Controller has independent snapshot and TOML profile parsers; it imports no Observer code. The
pipeline is snapshot checksum/schema verification, exact profile/recovery verification, built-in
never-touch evaluation, dry-run plan, then a transaction abstraction. No concrete TV mutation adapter
ships. Tests inject fake adapters.

Transactions journal one operation, run ADB/launcher/Settings/network health checks, then continue.
A failed check stops, reverts the last recorded operation, and permanently disables automatic retry
for that plan ID. Rollback selects only successful, unreverted Controller journal entries and is
idempotent. User APK management is a separate feature-gated boundary.
