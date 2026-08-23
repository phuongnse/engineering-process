# Production standard

`production-v1` is the portable minimum quality contract for every project governed
by engineering-process, including engineering-process itself. It is a change gate,
not a claim that every dimension needs the same implementation in every product.

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
least one measurable acceptance criterion. A project may declare sorted
`project-*` extensions in `.process/project.json`; extensions add to the core and can
never replace or weaken it.

## Evidence and observability

Evidence must identify the change, cycle, immutable checkpoint, workspace
fingerprint, actor/context, profile, selected impact, command digest, timestamps,
exit status, timeout state, output byte counts and output digests. Raw command output
is streamed for diagnosis but is not copied into lifecycle JSON, so reports stay
bounded and avoid becoming a secret store. Projects define redaction and retention
for their own application logs. Distributed tracing is required only when the
affected architecture needs it; every project still needs enough correlation to
explain a failed operation.

Environment probe regular expressions evaluate a bounded view whose CRLF and CR line
boundaries are canonicalized to LF. Captured output, byte counts, truncation state,
and digests retain the original bytes so portability does not weaken evidence.

Missing, stale, truncated beyond a declared policy, blocked, or unverifiable evidence
never becomes a pass. Independent review records each accepted dimension as
`verified`, `failed`, or `not-applicable-confirmed`; lifecycle submission compares
that evidence one-for-one with the change contract before approval.

Remote matrix claims require one bounded supplemental bundle per platform/runtime.
The schema-1 manifest binds the exact source and workflow checkpoints, automation
actor/context, run identity and URL, platform/runtime identity, selected impact,
configured timeouts, output byte counts/digests, truncation state, and the hashes of
its schema-2 profile reports. The remote artifact id and service-computed digest are
preserved with review evidence. These reports supplement the public N-1 lifecycle
authority; code under verification never promotes itself to lifecycle authority.

## Failure to invariant

A validated command, gate, release, adoption, or external-integration failure must
preserve exact bounded evidence and classify its owner before corrective mutation as
project-local, shared-process, operations-or-external, or missing product or
authorization input. Dependent candidates stay blocked. Shared defects are corrected
in the producer rather than wrapped or duplicated by consumers; project behavior
remains project-owned unless a portable class is proven.

Every correction proves valid behavior and the corresponding fail-closed class at the
lowest reliable owner boundary. Shared corrections additionally require producer
profiles and reproduction at affected consumer boundaries before release. A transient
operations/external recovery is allowed only with unchanged source and configuration;
attempts are bounded, idempotent, diagnostic-preserving, and stop on deterministic
failure. Source, branch, version, credentials, and controls are never changed merely
to cause another attempt. Independent review treats violation of these ownership and
evidence boundaries as completion-blocking.

## Resource and generated-state policy

Every operation over repository-controlled or remote input has explicit limits for
time, count, individual item size, aggregate size, output, and process descendants.
Limits fail closed and have regression coverage for success, failure, timeout, and
interruption. Selective verification reduces work only through the distribution-owned
impact algorithm; unmatched or ambiguous paths expand verification.

Authority adoption treats project configuration as consumer-owned declarative data.
A target-version migration binds exact source and target manifest digests, is size
bounded, contains no executable command, validates under the installed target
authority, and shares one rollback boundary with the process lock and managed assets.
Optional capabilities are never inferred, while configuration required by the target
authority blocks adoption when it is missing or invalid.

Released serialized contracts are never tightened in place. A new resource bound or
meaning-changing requirement uses a new integer schema major with explicit migration;
historical readers retain their published behavior. A new optional capability may
expand an existing schema major without invalidating its prior documents: portable
impact and quality declarations therefore remain additive on project schema 3. New
integrations use the bounded plan and project schema majors while older artifacts
remain readable as history.

Ephemeral files use private temporary directories and are removed on success,
failure, timeout, and interruption. Build outputs are created in an isolated tracked
snapshot and never persist in the source checkout. `.process/runs/` is durable local
lifecycle evidence, not temporary state: completed evidence is exported and
validated against `schemas/evidence-receipt.schema.json` and its semantic cross-links
before an explicit prune; active or unexported evidence is not deleted.
Verification isolates interpreter bytecode caches from the checkout and rejects
ignored sourceless bytecode that could shadow checkpoint-owned source.
Managed skill text is checked out with canonical LF through a bounded process-owned
`.agents/.gitattributes` file whose directory precedence is above project-root
rules. The closer rule also disables inherited working-tree encoding, filter, and
ident transforms that could otherwise rewrite managed bytes, and a self-rule gives
the attributes file the same byte-stable checkout policy. Deeper repository attribute
files remain subject to managed-tree ownership and content checks. Integrity comparison
remains byte-exact; newline variants are not accepted as alternate distribution bytes.

The producer repository applies the same LF and byte-transform isolation to its
own automatically detected text sources through a tracked root `.gitattributes`
policy. This keeps distribution input bytes platform-independent. The producer
root policy is not a consumer-managed asset and is never written by bootstrap or
sync.

## Release identity

The release contract is the source of truth for package name, distribution name,
SemVer, tag, GitHub release title, runtime version location, artifact names, and
lifecycle receipt name. Governed releases use the exact tag and title `v<SemVer>`.
Publication cross-checks every declared surface against one immutable checkpoint;
consumer locks change only after public artifacts and hashes are verified.
The exact public N-1 binary validates governed lifecycle receipts with no fallback to
code under release. Release/build dependencies are artifact-hash locked and the build
runs without dependency isolation or network resolution.

Immutable releases created before this contract use `bootstrap-history` provenance.
That mode records their actual identity and explicitly makes no lifecycle-governance
claim. It cannot be used for new governed releases.
