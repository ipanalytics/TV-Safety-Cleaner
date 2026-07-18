# Security

Every plan requires a checksum-valid schema 1.0 inventory snapshot, supported platform, exact model,
firmware and fingerprint, `verified = true`, and `Verified on this device` recovery status. Connection
and authorization gates are explicit. Unknown values refuse.

Built-in protected packages cover Android core, System UI, Settings, launcher, OTA, package manager,
installer, downloads, ADB/network, Wi-Fi, Bluetooth, HDMI/CEC, tuner/live TV, audio, storage, setup,
and recovery. The policy overrides profile content. Only confirmed-safe packages are plan-eligible.

APK archives reject traversal, symlinks, inflated content, and media/download roots. APK downloads
use an allowlisted HTTPS host, a 500 MiB limit, `.part` files, and atomic metadata/task writes. A
downloaded signature file is recorded for inspection but does not establish publisher trust.

ADB runs argument arrays without a shell. User APK changes require a disabled-by-default setting and
per-operation confirmation. Uninstall is limited to packages Android reports as third-party. Install
and update refuse known never-touch packages and packages Android reports as system packages. System
maintenance plans remain non-mutating and separate from APK sideloading.
