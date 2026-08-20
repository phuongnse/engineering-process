# Production standard

`production-v1` is the portable minimum quality contract for every project governed
by engineering-process, including engineering-process itself. It is a change gate,
not a claim that every dimension needs the same implementation in every product.

## Abstraction and ownership

This standard owns portable outcomes, trust and ownership boundaries, evidence
qualities, and failure semantics. It does not own provider names, operating-system
primitives, source-control commands, workflow syntax, serialized field layouts, or
consumer-specific rollout decisions. Those mechanisms belong to their public
contracts, integration guides, implementation modules, and tests; high-level policy
links to those owners instead of duplicating their details.

The process may require a project to declare and verify its compatibility, migration,
deployment, and retirement outcomes. The project remains the authority for selecting
those domain strategies. A process rule must not infer a strategy merely because one
implementation or example uses it.

Deterministic documentation checks register abstraction layers, validate dependency
direction, and reject implementation-shaped structure in high-level policy. They do
not claim to understand arbitrary prose. Independent review verifies that every
concrete mechanism remains with its declared owner; enforcement never depends on a
list of currently known providers, platforms, or tools.

Each project owns the representation of its document registry and binds the
structural check to a project verification profile. The portable process owns the
required layering outcome and semantic review boundary, not consumer filenames or
documentation tooling.

## Required dimensions

Every new change contract assesses these dimensions in canonical sorted order:

- `compatibility`: supported consumers, platforms, data, APIs, and migrations;
- `correctness`: observable outcomes, invalid input, and regression behavior;
- `maintainability`: ownership, cohesion, testability, documentation, and retirement;
- `observability`: bounded logging, tracing or correlation, metrics where useful,
  redaction, retention, and actionable failure evidence;
- `operability`: setup, configuration, deployment, rollback, recovery, cleanup, and
  deterministic failure handling;
- `performance`: input, time, memory, process, I/O, output, and scalability bounds;
- `privacy`: personal or sensitive data collection, access, minimization, retention,
  deletion, and disclosure;
- `reliability`: timeout, interruption, retry, idempotency, partial failure, and
  resilience behavior;
- `security`: trust boundaries, authorization, untrusted input, secrets, dependency
  execution, containment, and fail-closed behavior;
- `supply-chain`: source, dependency, evidence, artifact, release, and consumer-lock
  identity and provenance.

`correctness` is always applicable. Any other dimension may be `not-applicable` only
with a concrete rationale and no mapped criterion. An applicable dimension maps to at
least one measurable acceptance criterion. Projects may declare namespaced quality
extensions through the public project contract; extensions add to the core and can
never replace or weaken it.

## Evidence and observability

Evidence identifies the change, cycle, immutable source checkpoint, source identity,
actor and context, verification scope, operation identity, timestamps, outcome,
resource bounds, and integrity information. Diagnostic evidence is attributable,
bounded, and safe to retain; it does not silently collapse a failed identity probe
into an unexplained value. Start and completion evidence distinguish source mutation
from collection failure without weakening exact identity comparison.

Projects own redaction and retention for their application data. The process retains
only the bounded evidence needed to explain its own decisions and never treats an
evidence report as a secret store. Architecture-specific telemetry is required only
when the affected system needs it, while every failed operation still needs an
actionable and correlatable explanation.

[`ENVIRONMENT_CONTRACT.md`](ENVIRONMENT_CONTRACT.md) owns portable probe matching,
original-output evidence, and finite-command semantics.

Missing, stale, truncated beyond a declared policy, blocked, or unverifiable evidence
never becomes a pass. Independent review records each accepted dimension as
`verified`, `failed`, or `not-applicable-confirmed`; lifecycle submission compares
that evidence one-for-one with the change contract before approval.

Publication evidence is referenceable from the review object. A satisfied public
claim points to durable evidence owned by that claim; internal aliases, local paths,
or unpublished identifiers never substitute for a reference another reviewer can
follow. The publication contract owns the bounded reference representation.

Remote-environment claims require bounded supplemental evidence tied to the exact
source and verification identity. Integration contracts own provider and artifact
mechanisms. Supplemental reports add portability evidence but never allow code under
verification to promote itself to lifecycle authority.

## Resource and generated-state policy

Every operation over repository-controlled or remote input has explicit limits for
time, count, individual item size, aggregate size, output, and process descendants.
Limits fail closed and have regression coverage for success, failure, timeout, and
interruption. Selective verification reduces work only through the distribution-owned
impact algorithm; unmatched or ambiguous paths expand verification.

Authority adoption treats project configuration as consumer-owned declarative data.
A target-version migration binds its source and target identities, contains no hidden
execution, validates under the target authority, and shares one rollback boundary
with all process-owned assets. Optional capabilities are never inferred; missing
required consumer configuration blocks adoption.

Released serialized contracts are never tightened in place. Meaning-changing
requirements use an explicit compatibility boundary and migration, while additive
capabilities preserve historical readers. [`VERSIONING.md`](VERSIONING.md) owns the
exact package, schema, release, and adoption classification rules.

Ephemeral state is isolated and removed on success, failure, timeout, and interruption.
Durable lifecycle evidence is retained until an explicit, validated export and prune
boundary. Build and generated outputs cannot alter the source or authority being
verified. Managed assets remain byte-stable and integrity protected across supported
environments; portability mechanisms belong to their implementation owner.

## Repository integration policy

Every hosted project protects its default integration boundary through the portable
baseline in [`REPOSITORY_GOVERNANCE.md`](REPOSITORY_GOVERNANCE.md). Only the current
reviewed and successfully verified source may integrate; bypass and destructive
history changes are forbidden by default. Consumers declare policy, while adapters
provide exact enforcement without weakening the baseline or mutating external state
without separate owner authorization.

Public gate identities remain stable while project-owned verification evolves.
Missing, stale, ambiguous, or drifting integration evidence fails closed. Repository
protection cannot substitute for resolving a flaky verification operation.

## Release identity

The release contract is the single owner of release and artifact identity.
Publication cross-checks every declared surface against one immutable source; consumer
authority changes only after public artifacts and their integrity are verified. The
current public authority validates the next release evidence with no fallback to code
under release. [`VERSIONING.md`](VERSIONING.md) and the release integration guide own
concrete version and publication mappings.
