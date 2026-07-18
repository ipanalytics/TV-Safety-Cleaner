# Security

The ADB allowlist is `devices`, `properties`, `packages`, `disabled-packages`, `activity-summary`,
`package-summary`, `memory-summary`, `storage-summary`, `process-summary`, and `setting-read`.
The observation collector also permits fixed `network-summary` and `cpu-summary` reads.
Arguments are arrays and untrusted serials/keys are validated before process execution. There is no
arbitrary command endpoint. Output and timeout limits prevent accidental resource exhaustion.

The web services bind to ports 8090 and 8091 on all interfaces, then enforce an allowlist containing
only loopback and RFC 1918 private networks. Observer and Controller share one password hash and
secret. Password auth, CSRF on every POST, strict session cookies, upload limits, filename checks,
rate limits, and POST/Redirect/GET protect forms.

Store data only under `/srv/tv-safety-data`, never media/download paths. Keep configs and secrets
mode 0600, backups private, and dependencies pinned by the operator's deployment process. Verify
snapshots before reading, diffing, redacting, archiving, or handing them to Controller.

The shipped ADB adapter contains fixed reads only. This reduces technical risk but cannot determine
or guarantee the contractual warranty policy of a specific TV manufacturer.
