# Threat Model

Assets are ADB authorization, snapshot privacy and integrity, recovery evidence, local credentials,
and Raspberry Pi service availability. Attackers may control TV output, an import file, an HTTP
client on the LAN, a profile/archive, or a dependency source.

| Threat | Mitigation |
| --- | --- |
| Command injection | Fixed operation methods, argument arrays, validated serial/key values, no user command route |
| Malicious snapshot | schema/version checks, required artifacts, SHA-256, no symlinks, bounded names |
| Path traversal | relative import roots, resolved containment, basename checks, safe archive members |
| Symlink attack | imports and snapshot artifacts reject symlinks before use |
| Privacy leakage | excluded credential fields, local-only storage, redact copy, no external upload |
| LAN access | RFC 1918 CIDR allowlist, shared authentication, CSRF, rate limits |
| CSRF | unpredictable session token and constant-time validation on every POST |
| Firmware drift | fingerprint/firmware captured and Controller must exact-match a profile |
| ADB timeout/offline | bounded timeout and explicit state; no automatic mutation or retry loop |
| Huge output/import | ADB output and HTTP/import byte limits |
| Malicious APK | Observer does not inspect or install APKs; Controller archives are gated separately |
| Dependency supply chain | two declared runtime packages, isolated venv, operator-reviewed updates |

Residual risks include a compromised ADB binary or host, incomplete vendor data, behavior invisible
to fixed ADB reads, and recovery procedures that have not been manually tested.
