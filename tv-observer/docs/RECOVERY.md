# Recovery

Recovery readiness uses exactly: `Not checked`, `Documented only`, `Partially verified`,
`Verified on this device`, and `Failed`. Evidence must describe the exact device and firmware.
Documentation cannot mark a factory reset drill verified; only a completed manual device drill can.

Keep a checksum-valid inventory snapshot, restore plan, recovery plan, redacted support copy, account
setup notes, physical remote, and vendor recovery instructions. Test ADB reconnection, launcher,
Settings, network, HDMI/CEC, tuner/live TV, audio, storage, accessibility, and initial setup.

Snapshot inventory can reconstruct facts and guide manual recovery. It is not a firmware image, APK
archive, account backup, or guaranteed rollback. Observer never initiates a reset. See
`FACTORY_RESET_DRILL.md` for the manual checklist and warranty/data-loss warning.
