# Improving the process

The process may change only for a concrete consumer need.

A process change contract must name the consumer repository and a real incident,
failed adoption, repeated friction, or missing capability in consumerEvidence. The
  engineering-process project configuration enforces that field.

Process-owned artifacts and schemas have one current definition at version `1`.
When a consumer exposes a shared gap in those definitions, the correction edits the
current contract directly, including breaking changes. Unsupported release formats
are rejected; this intake does not authorize a compatibility reader, migration layer,
or fallback. Consumers update configuration, integrations, and generated artifacts
when adopting the released correction.

Choose the smallest correction:

1. delete obsolete behavior;
2. clarify the skill route;
3. repair existing enforcement;
4. add a gate only when the consumer evidence shows the first three cannot protect
   the required invariant.

Readiness creates a feedback loop, not autonomous mutation:

1. a consumer attempt exposes an incident, repeated friction, failed adoption, or a
   planned capability whose reusable invariant is missing;
2. consumer-specific behavior stays in that consumer;
3. the smallest shared correction follows the normal lifecycle and independent review;
4. package, release, and readiness identities remain immutable and pinned, so process
   adoption never substitutes one release's evidence for another;
5. the owner authorizes release and each consumer independently adopts it;
6. the resulting consumer behavior becomes evidence for the next bounded iteration.

The process does not scrape consumer data, choose product priorities, edit itself in
the background, merge its own change, publish itself, or auto-promote readiness.

## Consumer issue intake

An ordinary GitHub issue is the transport from a consumer-only checkout. The consumer
first fixes or safely blocks its current incident, then submits a sanitized, bounded
report through the `Consumer process improvement` form. Search and reuse an existing
issue before creating another. Issue creation is an owner-authorized external action,
never a consumer-CI responsibility and never a reason to grant a consumer or Renovate
token write access to this repository.

Both CLI and manual submission use the same non-sensitive
`[consumer-process][CONSUMER][PROCESS-VERSION][INVARIANT][INCIDENT-KIND]` title key.
Without `gh`, search all open and closed issues for that complete key before the form,
reuse an existing issue when present, and replace every title placeholder before submitting. If no issue
URL is available yet, a pending review remains `review-pending` awaiting owner creation;
draft files or search queries cannot serve as the required `recordUrl`.

The issue records consumer/process/pack identity, observed behavior, expected shared
invariant, publishable evidence, current mitigation, reusable rationale, and disclosure
confirmation. Private consumers must omit private source, raw logs, media, credentials,
tokens, and production metadata; a public issue may reference separately authorized
private evidence.

Finish-time automated intake collects only the same structured taxonomy. It performs
tracker I/O only when the consumer's process-change policy supplies an accepted issue
namespace; the process-producer recursion breaker suppresses its own writes. Search
errors, malformed matches, invalid writer URLs, and budget exhaustion are recorded as
bounded failures or suppressions, never treated as an empty search. A recorded result
for the same key is reused before another tracker call. The generated body contains
only allow-listed identifiers, counts, and full digests; reviewer rationale and arbitrary
runtime details are not published.

Accepted intake becomes the process change `source` and `consumerEvidence`. The issue
does not authorize implementation or block the consumer, and it closes only after a
released correction is adopted and validated in the originating consumer.

A process producer can set `lifecycle.processChanges.acceptedIssueUrlPrefix` to its
owner-selected HTTPS issue namespace. `change start` then accepts only that exact
prefix followed by a canonical positive integer, before any lifecycle state is
written. This local check prevents placeholders and cross-repository handoffs without
network access or consumer credentials; issue acceptance remains an owner decision.

Use an ordinary issue or pull request for cross-repository discussion. Then run the
same start, plan, implement, verify, review, finish lifecycle as every other change.
The producer does not need a federation protocol, catalog, recommendation chain, or
special authority transition to learn from a consumer.
