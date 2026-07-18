# Device Profile Guide

A profile directory contains `profile.toml`, `packages.toml`, `checks.toml`, and `recovery.md`.
Start from a fresh real-device Observer snapshot after firmware updates. Record exact platform, model,
build, fingerprint, recovery status, package evidence, health checks, and manual recovery results.

Package categories are confirmed-safe, probably-safe, telemetry-candidate, advertising-candidate,
risky, never-touch, and unknown. Only confirmed-safe is plan-eligible, and built-in protected packages
remain blocked. Do not mark a profile verified from model similarity, network labels, or documentation.
