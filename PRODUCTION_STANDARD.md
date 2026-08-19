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

## Resource and generated-state policy

Every operation over repository-controlled or remote input has explicit limits for
time, count, individual item size, aggregate size, output, and process descendants.
Limits fail closed and have regression coverage for success, failure, timeout, and
interruption. Selective verification reduces work only through the distribution-owned
impact algorithm; unmatched or ambiguous paths expand verification.

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
rules. Deeper repository attribute files remain subject to managed-tree ownership
and content checks. Integrity comparison remains byte-exact; newline variants are
not accepted as alternate distribution bytes.

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
