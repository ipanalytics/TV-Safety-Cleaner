# Recovery

Controller requires Observer recovery status `Verified on this device` before a system plan. A plan
must verify connection and authorization, apply one operation, check ADB, launcher, Settings and
network, then journal success. Failure stops and reverts only the last attempted operation.

`rollback --dry-run` shows successful unreverted Controller entries. `restore --dry-run` reports the
journal and excludes external changes. Rollback never guesses original state, never resets the TV,
and is idempotent. Inventory snapshots and restore plans are guidance, not firmware backups.

## User-package operation ledger

APK install, update, uninstall, enable, and disable actions record the exact device identity and
package before-state before mutation. A selective inverse is offered only when the live device and
package still match the recorded branch. Newer actions on unrelated packages remain active. A
newer pending, applied, or uncertain action on the same package blocks rollback until that branch
is resolved. An uncertain operation that already matches its before-state is reconciled without
issuing another TV mutation.

Named state profiles contain exact third-party versions and enabled states. Applying one requires
the same model, firmware, and build fingerprint and a retained exact APK for every changed version.
Application is sequential, stops on the first mismatch, and journals each mutation separately.
Extra packages are never removed automatically. Profiles are not firmware or user-data backups.
