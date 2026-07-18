# Contributing

Keep real mutation disabled unless exact profile, recovery, transaction, and manual hardware gates
are all reviewed. Never weaken the built-in protected policy. Add fake-adapter tests for every state
transition and run compileall, pytest, Ruff, and strict mypy. Do not add shared runtime code, external
uploads, generic command execution, or extra production dependencies without a security review.
