# Engineering Process Agent Contract

This repository owns a small, agent-neutral engineering process. Keep guidance in
portable skills and deterministic behavior in processctl plus JSON Schema.

## Rules

- The lifecycle is exactly start, plan, implement, verify, independent review, and
  finish. Do not create a parallel lifecycle or evidence chain.
- Never execute project commands through a shell. Commands are argument arrays with
  timeouts and bounded captured output.
- Reject review when either reviewer actor or reviewer context participated in the
  current implementation cycle.
- Consumer repositories own behavior, exact commands, merge policy, deployment, and
  release decisions. This distribution owns only lifecycle transitions and managed
  assets.
- Every runtime JSON document must validate against its packaged schema. Do not add a
  handwritten shape validator beside JSON Schema.
- A process change must cite evidence from a real consumer. Prefer deletion,
  clarification, or repair before adding another gate.
- Source skills under process_assets/skills are the next distribution. Managed
  .agents/skills represent the currently adopted public release and change only in an
  adoption pull request.
- Keep pre-1.0 doctor --profile and read-only publication adapters
  until existing consumers have adopted 1.x. Setup may run only consumer-owned
  argument arrays.

## Verification

Run:

    python verification/run_test_suite.py
    python processctl.py skills validate --root process_assets/skills
    python processctl.py release validate
    python verification/verify_distribution.py

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

Independent review requires an attested read-only actor and context that did not
implement the current cycle. No particular agent host is required. Missing or stale
evidence, self-review, and publication without separate authorization are blocking.
<!-- engineering-process:end -->
