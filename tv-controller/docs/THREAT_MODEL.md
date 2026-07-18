# Threat Model

| Threat | Mitigation |
| --- | --- |
| Forged/tampered snapshot | independent SHA-256, required checked files, schema and symlink guards |
| Malicious profile | strict TOML shape, exact identity match, verified/recovery gates, built-in policy override |
| Firmware drift | exact build and fingerprint comparison on every plan |
| Never-touch bypass | code-owned exact/token policy applied to all profile categories |
| Partial operation | one operation before health verification and durable journal |
| Retry loop | failed and completed plan IDs set retry disallowed |
| Rollback overreach | only unreverted Controller-recorded operations are selected |
| Malicious APK | ZIP path/count/expanded-size checks, metadata validation, system-app refusal |
| Path/privacy leak | private data roots outside media shares; no external upload |
| Supply chain | Flask and gunicorn only at runtime; standard library for domain logic |

Residual risk remains until transaction behavior, recovery, and package claims are manually verified
on the exact TV and firmware. A compromised host or ADB binary is outside the application's boundary.
