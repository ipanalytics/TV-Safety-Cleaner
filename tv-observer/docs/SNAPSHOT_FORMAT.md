# Snapshot Format

The default path is `/srv/tv-safety-data/backups/<device>/<local-timestamp>/`. A hidden temporary
sibling is used until every file is written, JSON parses, and SHA-256 verifies. Readers ignore hidden,
partial, invalid, and symlinked directories.

Required files include `manifest.json`, device and firmware facts, package/disabled package lists,
permissions, components, launcher, readable settings, processes, memory, storage, and network
summaries, `restore-plan.json`, `privacy-report.json`, `recovery-plan.md`, and `checksums.sha256`.
Schema version 1.0 is described by `snapshot-schema.json`.

Sensitive credentials, Wi-Fi secrets, cookies, tokens, passwords, and message contents are excluded
by default. Redaction creates a separate checksum-valid copy. Archive verifies the source before
packing safe relative members. This format is an inventory snapshot, not a full firmware backup.
