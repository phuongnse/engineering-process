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

Process-owned finite commands pass only when execution is otherwise successful and
the complete admitted stdout and stderr streams are free of classified warning and
error diagnostics. The distribution owns one bounded, non-configurable classifier;
an exit code of zero cannot override a diagnostic failure. Projects correct the
diagnostic at its owning boundary rather than suppressing output, changing the
canonical command, or installing a consumer wrapper. Durable reports record only the
policy, severity, stream, line number, bounded count, truncation state, and line
SHA-256. They never copy the diagnostic line into lifecycle evidence.

The portable logical command `python` resolves to the exact absolute interpreter of
the running immutable process authority across doctor, setup, execution, and
verification. Ambient `PATH` cannot select another Python installation, managed tools
cannot shadow the reserved binding, and evidence retains the logical command rather
than a host-specific activation sequence.

Missing, stale, truncated beyond a declared policy, blocked, or unverifiable evidence
never becomes a pass. Independent review records each accepted dimension as
`verified`, `failed`, or `not-applicable-confirmed`; lifecycle submission compares
that evidence one-for-one with the change contract before approval.

Remote matrix claims require one bounded supplemental bundle per platform/runtime.
The current schema-2 manifest binds the exact source and workflow checkpoints,
automation actor/context, run identity and URL, platform/runtime identity, selected
impact, configured timeouts, output byte counts/digests, diagnostic summaries,
truncation state, and the hashes of its schema-3 profile reports. Historical
supplemental schema-1 manifests and verification schema-1/schema-2 reports retain
their released reader semantics. The remote artifact id and service-computed digest
are preserved with review evidence. These reports supplement the public N-1
lifecycle authority; code under verification never promotes itself to lifecycle
authority.

Projects declare named remote requirements and their exact profile and
platform/runtime selector sets; change contracts select requirement ids. A lifecycle
request binds those expanded requirements to one clean checkpoint, comparison base,
workspace fingerprint, and immutable base-owned workflow checkpoint without granting
review or delivery authority. Provider adapters remain project-owned and transport
only the exact request. Core ingests local artifact bytes, verifies service and
content digests plus one-to-one selector coverage, and keeps the lifecycle
implementing until every local profile and remote requirement passes. A provider job
conclusion or later PR check is not lifecycle evidence by itself.

## Failure to invariant

A validated command, gate, release, adoption, or external-integration failure must
preserve exact bounded evidence and classify its owner before corrective mutation as
project-local, shared-process, operations-or-external, or missing product or
authorization input. Dependent candidates stay blocked. Shared defects are corrected
in the producer rather than wrapped or duplicated by consumers; project behavior
remains project-owned unless a portable class is proven.

Every governed verification failure and unresolved review finding has a structured
improvement disposition before corrective progress. A shared consumer case exports a
bounded untrusted signal and remains incomplete until a producer disposition,
completed-lifecycle and immutable-release resolution, and exact consumer reproduction
all validate. A failure assigned to an already resolved catalog invariant is a
recurrence and requires producer-owned process evolution or an explicit owner-approved
exception. See `PROCESS_IMPROVEMENT.md`.

Every correction proves valid behavior and the corresponding fail-closed class at the
lowest reliable owner boundary. Shared corrections additionally require producer
profiles and reproduction at affected consumer boundaries before release. A transient
operations/external recovery is allowed only with unchanged source and configuration;
attempts are bounded, idempotent, diagnostic-preserving, and stop on deterministic
failure. Source, branch, version, credentials, and controls are never changed merely
to cause another attempt. Independent review treats violation of these ownership and
evidence boundaries as completion-blocking.

## Recommendation validity

An opted-in project gates implementation through plan provenance. Every nontrivial
authored plan receives a fresh read-only semantic assessment for architecture,
authority, compatibility, external mutation, lifecycle order, owner, rollout, scope,
and trust boundary. The assignment binds the exact contract, plan, clean source,
authority, project policy, author, reviewer context, and canonical category set. A
clear assessment may authorize implementation; a decision-required assessment must
bind the existing independently reviewed recommendation and explicit owner resolution
for that exact assessment. Drift in any binding fails closed.
Each later implementation cycle after a finding or clean post-verification source
drift requires a newly reserved context and assessment bound to that cycle's source;
cycle-1 evidence cannot be relabeled or reused.

A process-generated plan bypass is accepted only when the immutable installed core
recognizes its generator and exactly recomputes the complete plan from bounded
validated source-owned inputs. Generator labels, risk labels, author
self-classification, heuristic prose scanning, and partial comparison are not
provenance. Exact generated plans are not universally sent to semantic review, and
portable core does not introduce a daemon, scheduler, webhook, hosted reviewer
platform, vendor, model, or proprietary agent API. Unreviewed prose remains
candidate-only and grants no decision or lifecycle authority.

The three possible review boundaries never review one another. Plan-decision review
assesses the authored plan; recommendation review challenges only the recommendation
and its digest binding; final lifecycle review assesses implemented source and current
evidence. Reviewer-of-reviewer, meta-assessment, assessment-of-assessment,
policy-for-policy, dynamically generated approval chains, and generic workflow
engines are outside the portable contract.

A material owner decision derives recommendation eligibility before optimization.
Every option assesses every governing hard invariant and references only proven or
explicitly unproven assumptions. Any violated invariant makes the option invalid; any
unproven invariant or referenced assumption makes it unproven. Only the complete
derived valid set may enter cost, convenience, minimal-change, rollout, or other
secondary ranking.

High-risk recommendations require a process-created digest-bound review assignment.
Before review, the reviewer method must match its actor kind, a non-participant host
must attest independence, and the context is atomically reserved in the same
project-global registry used by lifecycle review. The assigned independent
adversarial review covers assumption evidence, invariant tracing, option
classification, and terminal ordering.
A missing, stale, incomplete, self-attested, context-reused, changes-requested,
invalid, or unproven chain cannot produce a recommendation. When no valid option
exists, the outcome is `decision-required`, not an optimized compromise. Owner
resolution may select only a valid option and grants no lifecycle completion, merge,
release, deployment, or adoption authority.

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

Proposal merge authority derives from proposal origin and publication state. An
agent-host review object created only after full lifecycle completion may use standing
policy auto-merge for its exact approved head. A Renovate `process-adoption` proposal
is created before consumer-owner review and must instead fix automerge false,
`consumerOwnerMergeRequired` true, and post-merge mutation false. Lifecycle
completion, standing policy, provider state, and successful static checks never move
that proposal onto the agent-host route. The consumer owns review and manually
authorizes merge; merge is the terminal cutover.

Before candidate-owned commands, a protected-base immutable verifier independently
binds the actual base and exact head, producer release/tag/commit/attestation, source
and target process identities, requirements bytes, process lock, migration result,
complete selected managed-file set, grouped full-SHA action-pin-only workflow delta,
and verifier identity. Partial, stale, inferred, unauthorized, or post-merge-dependent
candidates fail closed. This portable boundary reuses ordinary Renovate generation,
managed adoption, consumer CI, review and branch protection. It does not introduce a
reviewer host, daemon, scheduler, generic workflow engine, dynamically generated
approval chain, meta-assessment, or reviewer-of-reviewer layer.

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

## Standing gated automation

A project-owned standing policy may authorize unattended commit, push,
review-object, exact-head merge, release, publication, deployment, adoption, and
ephemeral cleanup only after the existing owning gates pass. It never weakens
lifecycle completion, fresh independent review, exact head/base, required checks,
branch protection, consumer ownership, release identity, or destructive-target
validation. Provider automerge remains disabled for untrusted pre-completion
proposals.

Owner involvement is exceptions-only: a required capability or authority is
unavailable, bounded idempotent recovery is exhausted, or a material product/security
decision is missing. Pending checks, normal bounded retries, and routine authorized
merge or publication operations continue automatically with durable diagnostics.

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
