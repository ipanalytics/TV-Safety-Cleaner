# Manual Factory Reset Recovery Drill

Observer never starts a factory reset. Perform this checklist manually only after accepting data
loss and warranty risk:

1. Complete only the minimum initial setup.
2. Do not enter unnecessary personal accounts.
3. Enable Developer Options manually.
4. Enable ADB manually.
5. Approve the Raspberry Pi ADB host key.
6. Create and checksum-verify the baseline snapshot.
7. Test an ordinary restart manually.
8. Verify that ADB reconnects after restart.
9. Optionally install a separate user test APK manually; never alter a system APK.
10. Initiate the standard factory reset manually only after explicit owner approval.
11. Complete the minimum initial setup again.
12. Re-enable ADB manually and approve the host again.
13. Create and verify the second snapshot.
14. Compare baseline and post-reset snapshots.
15. Manually check launcher, Settings, network, HDMI, tuner, sound, and OTA behavior.
16. Only after all checks pass, mark reset `Verified on this device` with manual evidence.

Documentation alone may be marked `Documented only`; it can never produce
`Verified on this device`. That status requires evidence from a completed manual device drill.
