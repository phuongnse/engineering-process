---
name: improve-process
description: Change the shared process only in response to evidence from a real consumer and prefer removing complexity over adding governance.
---

# Improve the process

Before opening a process change, identify the consumer repository and the concrete
incident, failed adoption, repeated friction, or missing capability. Put that evidence
in the normal change contract. A hypothetical self-governance concern is not enough.

A planned readiness gap can supply evidence only when a real consumer attempt exposes
missing shared guidance or enforcement. Fix consumer-specific behavior in the
consumer. Promote only the reusable invariant, and publish changed pack requirements
under a new immutable pack version; never mutate a version already selected by a
consumer or make process adoption depend on upgrading that pack.

Find the smallest reusable correction. Prefer, in order:

1. delete an obsolete rule or surface;
2. clarify a skill route;
3. repair an existing deterministic check;
4. add a new gate only when the consumer evidence proves the other options cannot
   protect the required invariant.

Use **run-change** for the actual work and require the same independent final review
as any consumer change. Track cross-repository discussion in ordinary issues or pull
requests; do not create a second lifecycle or evidence federation. The process never
self-publishes or self-merges: the owner retains release and adoption authority, and
the next consumer result becomes evidence for another bounded iteration.
