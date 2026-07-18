# Contributing

Preserve the read-only boundary and add fake-runner tests for every ADB operation. Do not add a
generic command function, runtime dependency, external upload, or shared code with another service.
Run `python3 -m compileall tv-observer`, `python3 -m pytest tv-observer/tests`, and
`python3 -m ruff check tv-observer`.

Strict mypy is advisory until typed Flask/Werkzeug stubs are accepted as development dependencies;
the security gate is Ruff plus runtime and adversarial tests. New domain modules should nevertheless
use dataclasses, enums, and explicit return types.
