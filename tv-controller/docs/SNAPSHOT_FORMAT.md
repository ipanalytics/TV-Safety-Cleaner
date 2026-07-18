# Snapshot Format

Controller independently reads Observer schema 1.0 inventory snapshots. Every referenced file must
have a valid SHA-256 entry; extra, unchecked, nested, or symlinked entries refuse. `manifest.json` and
`restore-plan.json` are mandatory. Platform, model, firmware and fingerprint feed exact profile gates.

The snapshot remains an inventory and plan source, not a full firmware image or guaranteed rollback.
Keep it under `/srv/tv-safety-data/backups`, never a media or download share.
