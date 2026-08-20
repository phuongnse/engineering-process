<!-- engineering-process:start -->
## Engineering process

Use the portable skills pinned by `.process/process.lock` for every non-trivial
change. Enter through `run-change` and use `processctl change ...` for specification,
planning, implementation registration, checkpoint verification, independent review,
finding resolution, and completion.

The project owns product decisions, domain contracts, exact verification commands,
and publication authority. The process distribution owns lifecycle semantics and
managed skills. Do not edit managed skills in this repository; update the pinned
distribution and synchronize them instead.

Keep durable guidance at its declared abstraction layer. High-level policy states
outcomes, invariants, ownership, and failure semantics; provider, platform, command,
workflow, and serialized-layout details stay with their contract, adapter,
implementation, or test owner. Product compatibility, deployment, migration, and
retirement strategies remain project decisions and are never inferred from examples.
Use registered layers and independent semantic review rather than a list of current
technologies to preserve this boundary. The project owns its document registry and
structural verification command; the process owns the required outcome.

Independent review requires an attested read-only actor and context that did not
implement the current cycle. No particular agent host is required. Missing or stale
evidence, self-review, and publication without separate authorization are blocking.
<!-- engineering-process:end -->
