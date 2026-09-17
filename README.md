# Engineering Process

Engineering Process is a small, agent-neutral way to drive repository changes through
one auditable path:

    start → plan → implement → verify → independent review → finish

Portable skills explain what to do. processctl owns state transitions and current
evidence. Each consumer owns its product rules, exact commands, merge policy, and
release decisions.

Start delivery work with [deliver-change](process_assets/skills/deliver-change/SKILL.md).
It selects the current phase from lifecycle state; callers do not select a phase skill:

    deliver-change
      change-start -> change-plan -> change-implement -> change-verify
        -> change-review -> change-complete

[process-improve](process_assets/skills/process-improve/SKILL.md) handles a reusable
process problem, then returns delivery to the same lifecycle.
[production-engineering](process_assets/skills/production-engineering/SKILL.md) supplies
the invariant floor consulted from planning through independent review. Both are
reachable specializations; neither advances lifecycle state. `change-complete` calls
the existing `processctl change finish` command.

The current skill catalog is the only supported catalog. Historical skill identifiers
belong to the release that published them and are not runtime aliases; see
[Versioning](VERSIONING.md#current-contract-boundary) for the adoption boundary.

## Architecture

The distribution has four live parts:

1. Managed skills under process_assets/skills, all reachable from deliver-change.
2. One processctl state machine under engineering_process/lifecycle.py.
3. JSON Schemas that are loaded directly by the runtime.
4. One adoption transaction that synchronizes managed skills and configuration from
   an exact hash-locked package.

Lifecycle state is local under .process/runs and completion creates one bounded receipt
under .process/receipts. Both paths are ignored by Git. Verification evidence is bound
to HEAD plus a fingerprint of tracked and non-ignored untracked files. Any relevant
mutation invalidates it.

Neither the reviewer actor nor reviewer context may have implemented the current
cycle. An agent reviewer context also cannot be recorded in another accepted change
in this repository or its registered worktrees. There is no attestation hierarchy,
recommendation chain, authority-transition protocol, remote-evidence federation, or
second handwritten validator.

The coordinator hands each new change to a newly spawned reviewer, with the accepted
source artifacts and no inherited implementation or other-change review conversation.
The same reviewer continues only that change's corrections. The reviewer inspects the change and authors its
own report. The runner's existing task/session interaction and returned result make
that work inspectable. Sharing a provider or account is allowed; this is a
workflow for independent judgment, not authenticated identity or merge enforcement.
See [change-review](process_assets/skills/change-review/SKILL.md) for the handoff.

Start, submission and finish check schema-valid canonical run history with bounded
worktree/file reads. A short OS lock in common Git metadata serializes canonical
history writes and review checks; profiles run outside it and reload current state
before recording results. The lock stores no identity registry. The check rejects known context reuse,
but cannot establish native conversation freshness or inspect removed history,
other clones or other repositories. Keep native creation, non-inherited dispatch
and effective settings evidence in the existing handoff.

Verification keeps two caller intents explicit. `change verify --profile PROFILE`
always refreshes the named profile. `change explain` is read-only; it shows the
accepted required profiles, valid prior evidence, remaining work, inapplicable
optional profiles, and blocked or unknown decisions. `change verify --remaining`
executes only remaining work and reuses a complete profile report only when the
candidate, contract/plan, project policy, process authority, runtime, and bounded
  child environment match. Evidence without that identity is unknown and reruns. This is
whole-profile reuse: equal check IDs and ordered side effects are never merged, and
the lifecycle receipt still records only actual executions.

Reuse is stage-aware rather than a blanket cache: the contract and plan travel by
digest; valid whole-profile verification can satisfy a continuation request; review
and finish consume the same reports after rechecking the snapshot; and a new
implementation cycle invalidates verification. Operation-local calculation reuse is
allowed only before a mutation/concurrency boundary. Impact feedback and generated
documents remain derived artifacts and never replace final required profiles or
independent review.

### Performance evidence for the 3.0.0 follow-up

The measurements below were taken on 2026-09-17 with the same Windows workspace
runtime and checked-in commands. They are observations, not a speed target:

| Scenario | Baseline `7dd2125` | Candidate `1729c19` | Interpretation |
| --- | ---: | ---: | --- |
| Development profile (`run_test_suite.py`) | 341.098s; 314 tests | 335.331s; 320 tests | Assurance changed because six regressions were added; no performance improvement is claimed. |
| Four review checks | 38.576s, direct commands | 35.827s, lifecycle profile | Close but not identical wrapper paths; no process-overhead improvement is claimed. |
| Continuation/reuse fixture | 5.803s | 5.561s | Same one-test fixture; no meaningful change. |
| Correction-cycle fixture | 3.992s | 4.090s | Same one-test fixture; no meaningful change. |
| Adoption convergence fixture | 1.185s | 1.107s | Same one-test fixture; no meaningful change. |

The candidate lifecycle recorded 370.936s for development and 35.827s for review;
the development value includes the bounded lifecycle runner around the consumer
suite. The process does not add a cache or telemetry system. These measurements
prove the compared observations and limits only; host variance, cache state, and the
different test count prevent a stronger end-to-end optimization claim.

Consumers that can prove complete changed-path coverage may opt selected required
profiles into final impact assurance with `impactProfiles.schemaVersion: 1` and
`finalProfiles`. Each opted profile must declare an explicit global unit whose paths
include the universal `**` pattern for cross-cutting reach. The lifecycle then
records the selected units as final
verification evidence; missing or unresolved coverage blocks rather than silently
falling back. A policy without `finalProfiles` remains feedback-only; this is one
current policy shape, not a second contract generation.

`processctl change review replace-reused` repairs only an initial pending assignment
proven to reuse another change's agent context, before a submitted review or normal
report file exists. It validates a fresh replacement and current verification,
preserves the prior assignment and conflict in canonical history, and leaves cycle
and evidence unchanged. Valid assignments and reviewed correction cycles cannot use
this operation. It does not provide an unrestricted reviewer reset.

Every delegated agent and reviewer follows the active user-selected model and
reasoning effort under [deliver-change's settings rule](process_assets/skills/deliver-change/SKILL.md#preserve-agent-execution-settings).
It covers fresh agents and resumed sessions, requires native runtime confirmation,
and permits no autonomous upgrade or downgrade. The portable guidance does not make
processctl a provider runtime or a model-quality evaluator.

Consumers using the packaged publication policy can enable
`lifecycle.publication.required: true` in `.process/project.json`. Start then rejects an invalid branch before creating a
run. Before final profiles run and before review assignment, the lifecycle checks
the current branch, head subject, and nonempty pinned comparison-base-to-head range,
and requires all candidate changes to be committed. Commit before `change verify`;
a later commit changes the checkpoint even when its source content is identical.
Local lifecycle run/receipt files and Git-ignored files are excluded. Finish repeats
the same checks and writes optional publication metadata in the single current
version-1 receipt. Missing pinned bases and mutations during the check fail without
advancing the lifecycle.

PR readiness additionally requires the selected ready-state body/title and exact
head/range checks. Follow the [completion preflight](process_assets/skills/change-complete/SKILL.md)
and repeat those checks in required CI. This repository's existing
`verification/verify_publication.py --pull-request` uses the actual PR metadata and
selected consumer standard; local lifecycle completion alone does not establish
external PR readiness. Integration-branch pushes remain consumer-owned operations.

Runtime architecture is enforced by semantic fitness functions, not module or source-
line quotas. Every module has an explicit dependency layer, imports point toward lower
layers, the internal graph remains acyclic, and lifecycle.py alone owns state
transitions behind the CLI adapter. Size metrics may guide refactoring but do not
decide correctness or release eligibility.

## Consumer configuration

Python 3.11 or newer and Git are required. A consumer owns .process/project.json:

    {
      "schemaVersion": 1,
      "project": "my-project",
      "lifecycle": {
        "requiredProfiles": ["development", "review"]
      },
      "setup": [
        {
          "id": "prepare-project-tool",
          "run": ["npm", "rebuild", "native-tool"],
          "timeoutSeconds": 300
        }
      ],
      "profiles": {
        "development": [
          {
            "id": "tests",
            "run": ["python", "-m", "unittest"],
            "timeoutSeconds": 600
          }
        ],
        "review": [
          {
            "id": "package",
            "run": ["python", "-m", "build"],
            "timeoutSeconds": 600
          }
        ]
      },
      "impactProfiles": {
        "schemaVersion": 1,
        "profiles": {
          "development": [
          {
            "id": "unit-tests",
            "run": ["python", "-m", "unittest", "tests.test_orders"],
            "timeoutSeconds": 600,
            "paths": ["src/orders/**", "tests/test_orders.py"]
          },
          {
            "id": "cross-cutting",
            "run": ["python", "-m", "unittest"],
            "timeoutSeconds": 900,
            "paths": ["**/process-policy.json"]
          }
          ],
          "review": [
          {
            "id": "package",
            "run": ["python", "-m", "build"],
            "timeoutSeconds": 600,
            "paths": ["src/**", "pyproject.toml"]
          }
          ]
        }
      }
    }

Commands are argument arrays, never shell strings. Each command has a finite timeout.
Output has a hard aggregate budget; evidence stores byte counts and hashes, never raw
stdout or stderr that could contain secrets.

For a fresh checkout of this repository, create a virtual environment with a supported
Python and install `engineering_process/requirements-runtime.txt`,
`engineering_process/requirements-dev.txt`, and
`engineering_process/requirements-build.txt` through that interpreter. Use
`.venv/Scripts/python.exe` on Windows or `.venv/bin/python` on POSIX to invoke the
source CLI and verification scripts. Other consumers own their runtime paths and
setup commands; installed consumers can use their environment's `processctl` entry
point without a source checkout.

The bounded runner prepends the invoking Python executable's directory to child
`PATH`. Bare commands therefore search that environment before the inherited path,
while declared argument arrays, explicit executable paths, secret filtering and
remaining path entries are preserved. This alignment does not install dependencies.

A failed profile also reports a safe `diagnostic` descriptor containing only its
validated profile identifier, check identifier, canonical one-based profile position,
and a fixed `processctl` argument array. The position remains unambiguous even when a
compatible project has duplicate check identifiers. Run that array from the project
root to reproduce only the failed configured check through the same bounded runner,
for example:

    processctl verify --profile rust --check-position 3

This selective run is diagnostic only: it does not expose captured output, replace a
required full-profile run, or count as lifecycle verification evidence. Maintainers
who need semantic tool output must use a tool-owned structured report or run the
consumer command in an appropriately trusted environment.

For a long-running check, opt into safe in-progress status with
`processctl verify --profile PROFILE --progress` or
`processctl change verify --change-id ID --profile PROFILE --progress` (also supported
for `--remaining` and `--affected`). Status is written to stderr at a bounded cadence,
so `--json` stdout remains one machine-readable result. It identifies the profile,
check position, elapsed time, timeout, last runner observation, response state and
captured byte count. A responsive runner is not proof that the suite is making
progress: when the command exposes no trusted internal signal, progress is explicitly
`unknown`; the status never invents a percentage, current test or remaining time. The
status stream is operational context only and cannot satisfy verification, review or
merge conditions.

### Production readiness

A consumer declares `.process/readiness.json` with production as its direction, its
current stage, immutable pack versions, and the state of every required capability.
An enforced capability maps to project-required profiles; a planned capability names
the concrete gap without pretending to have evidence:

    {
      "target": "production",
      "stage": "production",
      "packs": [{"id": "library-cli", "version": 1}],
      "capabilities": [
        {"id": "correctness", "state": "enforced", "evidenceProfiles": ["development"]},
        {"id": "runtime-safety", "state": "enforced", "evidenceProfiles": ["development"]},
        {"id": "compatibility", "state": "enforced", "evidenceProfiles": ["development"]},
        {"id": "portability", "state": "enforced", "evidenceProfiles": ["development", "review"]},
        {"id": "installability", "state": "enforced", "evidenceProfiles": ["review"]},
        {"id": "distribution-integrity", "state": "enforced", "evidenceProfiles": ["review"]},
        {"id": "adoption-integrity", "state": "enforced", "evidenceProfiles": ["development", "review"]}
      ]
    }

`project validate` and `doctor` resolve that declaration to the exact checks owned by
the consumer. The declaration does not make a weak command sufficient: normal CI and
independent review still judge whether those commands prove the named capability. A
building consumer may keep planned gaps while ordinary development continues. A
production-stage declaration fails closed if any capability remains planned.

The sidecar is a deliberate self-hosting boundary. Public authority N continues to
validate the adopted consumer files while source N+1 validates and self-applies the
current readiness contract. Adoption keeps the consumer-owned sidecar in place, but
the current runtime does not treat another release's files or evidence as interchangeable.
Pack versions are also immutable: a process update must keep `operations@1` working
even after `operations@2` exists. Process adoption and pack upgrades are separate
consumer-owned changes, preventing a new standard from deadlocking authority adoption.

`library-cli@1` was derived from this repository as a real producer and self-consumer.
`operations@1` was then derived from renovate-ops and requires auditability, automation
correctness, bounded execution, least privilege, policy integrity, recovery, and
target-selection integrity. `desktop-media@1` is derived from LyricRail and keeps its
existing correctness, input, source-portability, audit, media, package, and recovery-
mechanism evidence enforced. Stable dependency/recovery claims, signing, key custody,
runtime/license delivery, Linux advisory resolution, real-host workspace security,
updater, incident recovery, and independent security review remain planned.
Consumers without readiness remain at their own declared stage; readiness state does
not create a process-contract compatibility promise.

### Design quality

Changes that materially alter logic, state, or collaboration boundaries carry
consumer design standards in their existing acceptance criteria. The shared
[design quality guidance](process_assets/skills/production-engineering/SKILL.md#design-quality)
requires understandable responsibilities, data and state, ownership, and contracts.
Agents actively introduce or refine cohesive abstractions when current requirements
justify them, weighing comprehension and change locality against indirection.
Consumer architecture and language choices remain authoritative.

Start defines scoped design outcomes; the plan explains material choices in its
existing approach and work items; implementation revisits the affected flow before
verification. Independent review traces behavior and a concrete maintenance scenario
against the actual code. Demonstrated violations of accepted criteria can block
completion despite passing tests. Routine edits stay proportional, and a clear
direct implementation remains valid. The existing lifecycle enforces criterion-bound
findings and evidence freshness; contextual design quality remains the reviewer's
judgment, without another gate, artifact, or canonical invariant.

### Production engineering invariants

Every new plan and independent review applies one small canonical invariant floor:

- authoritative structure for open-world decisions;
- one authoritative source for shared policy;
- bounded, least-authority side effects;
- explicit current-contract boundaries and consumer recovery actions;
- assurance bound to current objective evidence and independent judgment.

The canonical triggers, required structures, prohibited failures, and expected
evidence live once in the managed `production-engineering/invariants.json` asset.
They are cross-domain invariants, not a catalog of preferred design patterns. A
closed, owner-versioned protocol may use literal state or enum tables; automation
must not guess open-world meaning from keywords, identifiers, filenames, diagnostic
text, or growing exception lists.

The current change, plan, review, receipt, selection, and run contracts all use
schemaVersion 1. They require the latest invariant assessments, current review
dispositions, exact snapshot/evidence identities, and literal repository-relative
`workItems[].affectedPaths` boundaries. A document from another release is rejected;
its version-1 marker is not evidence that it can substitute for a document from this
release. Before final verification, review assignment, and finish, the runtime rejects
a candidate path outside the declared boundaries and ignores only the exact
contract/plan input paths recorded as process control inputs. This proves scope
alignment, not root-cause correctness or minimality.

The plan and implementation skills require a causal chain from observed behavior or
risk through the violated contract, actual mechanism, smallest sufficient boundary,
and falsifiable evidence. Independent review must inspect that chain and the complete
diff. Schema validity, status booleans, check counts, coverage numbers, and green
profiles establish only their observed structural or execution properties; they do
not replace that semantic judgment.

This assessment is not a production certificate. Production still requires the
consumer's immutable readiness pack, every required capability in `enforced` state,
fresh consumer-owned verification on the exact candidate, and independent review.

For each ordinary change, `deliver-change` first surfaces this readiness view. The accepted
request and consumer rules determine which capabilities are affected. Every change
retains the project's baseline `requiredProfiles`; start and plan add any conditional
evidence profiles needed by affected capabilities and include a planned gap only when
the accepted request explicitly selects it. Implement and review protect the enforced
floor. A planned-to-enforced promotion is a reviewed consumer source diff with fresh
evidence. Unrelated planned gaps remain visible but do not block development, and no
skill chooses product priorities or changes readiness automatically.

When a consumer incident exposes a reusable process gap, `process-improve` first keeps
the consumer safe, then prepares a sanitized GitHub issue draft from that checkout.
It deduplicates by the complete current key
`[consumer-process][CONSUMER-KEY][PROCESS-VERSION][INVARIANT][INCIDENT-KIND]`, where
`CONSUMER-KEY` keeps ASCII letters, digits, dots, and hyphens and encodes every other
UTF-8 byte as `_hh` (for example, `phuongnse/lyric-rail` becomes
`phuongnse_2flyric-rail`). It requires
owner authorization before `gh issue create`, and uses an accepted issue as the later
process change source and `consumerEvidence`. Finish-time intake collects structured
signals for the same taxonomy, but expected activity is not an incident by itself: one
correct evidence invalidation, an explicit refresh, a rerun after correction, a
reopened completed change, or one ordinary review correction remains lifecycle history
unless the current cycle shows repeated abnormal behavior. Intake sends nothing unless
the consumer's process-change policy enables the tracker boundary. Search failure is
recorded as failure, not as an empty search; an existing run result is reused before
any retry. No raw reviewer prose,
private paths, consumer-CI write token, automatic process mutation, or wait for a
process release is required to continue consumer development.

Pin the process in requirements/process.in:

    --only-binary :all:
    engineering-process==3.0.0

Generate requirements/process.txt with hashes, install that lock, then run:

    processctl adoption apply \
      --project-root . \
      --requirements-lock requirements/process.txt

The transaction writes only managed surfaces:

- .agents/skills/<distributed-skill>
- the marked block in .github/PULL_REQUEST_TEMPLATE.md
- .process/adopt-process.py
- .process/process.lock
- the marked engineering-process block in AGENTS.md
- the current .process/project.json contract and managed configuration

It materializes the current managed skill catalog and preserves consumer-owned skills
and instructions. Applying the same current package twice is a no-op; a lock or
configuration from another contract release is rejected and must be recreated by the
consumer.
Consumer-owned `.process/standards.json` selections and override definitions are also
preserved; the managed PR template follows the effective supported standard. See
[consumer document standards](ARTIFACT_STANDARDS.md).
[Issue records](ARTIFACT_STANDARDS.md#issues) use the same default-and-override
mechanism for open requests and closed evidence while tracker actions remain consumer-owned.
[Automation naming](ARTIFACT_STANDARDS.md#automation-names) uses the same current
selection and override mechanism; consumers apply it in their bootstrap and provider checks.
The adopter does not inspect or delete superseded files or old managed
surfaces. Consumers remove unsupported files, update the current configuration and
integration references, regenerate current artifacts, and rerun their required
profiles as part of adopting a release. The Windows Job Object helper remains a
managed runtime-containment asset.

The three authored lifecycle documents stay intentionally small. A change contains
source, scope, outcomes, and profiles; a plan binds its digest; a review binds the
assigned checkpoint:

    {
      "schemaVersion": 1,
      "id": "change-123",
      "summary": "Deliver the accepted behavior",
      "source": "issue-123",
      "comparisonBase": "main",
      "risk": "medium",
      "affectedProjects": ["my-project"],
      "acceptanceCriteria": [
        {"id": "works", "outcome": "The observable behavior works"}
      ],
      "requiredProfiles": ["development", "review"]
    }

    {
      "schemaVersion": 1,
      "changeId": "change-123",
      "contractDigest": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
      "approach": "Implement through the existing owner.",
      "workItems": [
        {
          "id": "implementation",
          "outcome": "Deliver and prove the behavior",
          "affectedPaths": ["src/", "tests/"]
        }
      ],
      "risks": [],
      "productionEngineering": [
        {
          "id": "authoritative-structure",
          "applicability": "not-applicable",
          "rationale": "The change does not classify an extensible vocabulary.",
          "evidenceWorkItems": []
        },
        {
          "id": "single-policy-authority",
          "applicability": "not-applicable",
          "rationale": "The change introduces no shared policy authority.",
          "evidenceWorkItems": []
        },
        {
          "id": "bounded-side-effects",
          "applicability": "not-applicable",
          "rationale": "The change introduces no resource-bearing side effect.",
          "evidenceWorkItems": []
        },
        {
          "id": "contractual-evolution",
          "applicability": "not-applicable",
          "rationale": "The change does not alter a persisted or public contract.",
          "evidenceWorkItems": []
        },
        {
          "id": "evidence-bound-assurance",
          "applicability": "applicable",
          "rationale": "Completion must be proven on the exact candidate.",
          "evidenceWorkItems": ["implementation"]
        }
      ]
    }

    {
      "schemaVersion": 1,
      "changeId": "change-123",
      "reviewer": {
        "actorId": "review-agent",
        "contextId": "review-123",
        "kind": "agent"
      },
      "checkpoint": {
        "head": "0000000000000000000000000000000000000000",
        "fingerprint": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
        "fileCount": 1,
        "byteCount": 1
      },
      "verdict": "approved",
      "summary": "Accepted outcomes and evidence are complete.",
      "findings": [],
      "productionEngineering": [
        {
          "id": "authoritative-structure",
          "status": "not-applicable",
          "rationale": "No open-world classification is present.",
          "evidence": []
        },
        {
          "id": "single-policy-authority",
          "status": "not-applicable",
          "rationale": "No shared policy authority is present.",
          "evidence": []
        },
        {
          "id": "bounded-side-effects",
          "status": "not-applicable",
          "rationale": "No resource-bearing side effect is present.",
          "evidence": []
        },
        {
          "id": "contractual-evolution",
          "status": "not-applicable",
          "rationale": "No persisted or public contract changed.",
          "evidence": []
        },
        {
          "id": "evidence-bound-assurance",
          "status": "satisfied",
          "rationale": "Required profiles passed on the assigned snapshot.",
          "evidence": ["development and review profile reports"]
        }
      ],
      "processImprovement": {
        "status": "none",
        "rationale": "No reusable shared-process problem was observed."
      }
    }

`change review start` returns the current `reportSchemaVersion: 1` for its assignment
and bounded `processSignals` derived from existing lifecycle events. Signals are
prompts for independent judgment, not evidence that hidden external actions occurred;
they do not authorize tracker writes or conclude that a shared-process defect exists.
Priority records impact if unresolved, while severity controls the current lifecycle
gate. Every non-blocking finding records one disposition: `resolved` with a rationale,
or `accepted-risk` / `tracked-follow-up` with a rationale, owner, and stable HTTPS
`recordUrl`. The report also records the production-engineering assessment and a
`processImprovement` classification of `none`, `consumer-specific`, or
`shared-process`. A shared-process report requires an existing, owner-authorized issue
URL; without it, the review remains pending. Reports from another release are not
readable or reusable, even when they carry the same version-1 marker.

The [finding priority definitions](process_assets/skills/change-review/SKILL.md#finding-priority)
are the canonical P0-P3 impact convention for this process, including examples and
their relationship to blocking decisions.

## Running a change

Create and validate a change contract, then register it:

    processctl contract validate --kind change change.json
    processctl change start \
      --actor implementation-agent \
      --context change-123 \
      --contract change.json

Register a plan bound to the returned contract digest:

    processctl change plan \
      --change-id change-123 \
      --actor implementation-agent \
      --context change-123 \
      --plan plan.json

Register implementation and run every required profile:

    processctl change implement \
      --change-id change-123 \
      --actor implementation-agent \
      --context change-123
    processctl change verify --change-id change-123 --profile development
    processctl change verify --change-id change-123 --profile review

When continuing an incomplete verification, inspect and execute the necessary set:

    processctl change explain --change-id change-123
    processctl change verify --change-id change-123 --remaining

The explicit `--profile` form remains a refresh and is never silently converted to
reuse.

If implementation discovers paths outside the frozen plan, verification records a
`plan-scope` blocker and stops before approval. Do not edit the accepted plan or keep
retrying the same check. When the accepted outcome is unchanged, an owner can prepare
a new current-v1 contract with `supersedes` pointing to the blocked run and reason
`missing-plan-boundary`; `change start` preserves the old run by path/digest and keeps
its exact comparison base. The new plan must cover the complete inherited diff. The
new run starts without old verification, approval, findings, or receipt. If the
outcome changes, use a fresh accepted contract and decision instead.

Failed required verification is kept in the same run. Its safe diagnostic identifies
the profile/check and position, exit result, timeout/output/stream/cleanup indicators,
bounded stream metadata, fixed reproduction arguments, report digest, recorded time,
and candidate checkpoint. `change explain` gives a typed reference to the run and
labels the descriptor current, stale, or unavailable; the run path exposes the
schema-owned descriptor. It never persists raw output or guesses a test failure. Remaining-work
execution blocks a failed report on the same candidate/input identity, while changed
inputs or a deliberate explicit profile refresh remain separate from evidence reuse.
Spawn and other execution-condition failures record the missing consumer action. These
lifecycle blockers are distinct from consumer behavior, owner decisions, and any goal
harness retry counter.

`change status` also reports bounded recovery measurements: blocked remaining attempts,
explicit failed-report refreshes, remaining executions after invalidation, total profile
executions, and check launches. They support before/after acceptance scenarios without
introducing telemetry or turning diagnostic/module runs into required evidence.

For fast feedback, consumers may declare the current `impactProfiles` policy in
`.process/project.json` and run only units related to the candidate paths:

    processctl change explain --change-id change-123 --impact
    processctl change verify --change-id change-123 --affected --affected-profile development

Each impact unit owns an exact command and path-pattern coverage. Matching is
explicit and deterministic; overlapping units all run. A unit with
`scope: "global"` may subsume narrower units only when its declared paths include
the universal `**` pattern; a narrower pattern never covers unrelated paths. Every
changed path must resolve for
each requested profile. Missing policy, unsupported policy, or an unmapped path is
`unresolved`/`unavailable`: the process launches nothing and returns the action
`inspect-diff-and-update-impact-policy`. The agent must inspect the diff and
dependency reach, update the consumer-owned mapping or obtain an owner decision,
then repeat the explanation. It must not use a full profile as a fallback.

Affected execution under the current policy is feedback evidence only and never
advances the lifecycle or satisfies a required profile. A consumer that can prove
complete coverage may add `finalProfiles` to that same version-1 policy; `--remaining`
then records the selected units as `impact-assurance` evidence. Each opted profile
needs an explicit global unit with the universal `**` pattern for cross-cutting reach,
and unresolved final coverage blocks rather than falling back silently. Explicit
`--profile` remains the full refresh. Consumers without the declaration retain the
existing final boundary; the affected command does not guess a policy for them.

Assign an independent reviewer and submit its report:

    processctl change review start \
      --change-id change-123 \
      --actor review-agent \
      --context review-123

Write the report at the returned reportPath (under .process/runs, so it does not
change the reviewed snapshot), then submit it:

    processctl change review submit \
      --change-id change-123 \
      --review .process/runs/change-123/review-1.json

changes-requested returns to implementation and increments the cycle. The first
review is bounded by the frozen acceptance criteria. The same reviewer
checks correction diffs; new blockers are admitted only for remediation regressions
or a reasoned P0/P1 miss inside the original contract. A third changes-requested
review blocks the change after two correction cycles—it never waives review.

approved can finish only while the repository still matches the reviewed snapshot:

    processctl change finish \
      --change-id change-123 \
      --actor coordinator \
      --context finish-123

New runs preserve the accepted `comparisonBase` ref and contract digest, and record
its resolved commit separately as `comparisonBaseCommit`. Start rejects missing or
non-commit refs before writing the run. Lifecycle output supplies the recorded commit
for review even after commits or branch movement. Runs without the current pinned-base
and scope fields are rejected; recreate them under the current contract rather than
inferring a historical boundary.

Correction reports must retain every previously open blocker until its unchanged
identity receives an explicit `resolved` disposition. Omitting it or changing it to
`accepted-risk` or `tracked-follow-up` cannot retire the blocker. This applies to the
single current review contract.

### Public pull-request evidence

The default pull-request standard keeps public assurance separate from local
lifecycle identity. Its three purpose-led sections are ordered and stable: the
reader-facing result; contract and current evidence; and review plus completion.
Only information needed to understand impact, assurance, and the completion decision
is shown by default; local identities and incidental dependency boilerplate stay out
of the public body.
The public description never needs an actor ID, context ID, reviewer
handle, or local `.process/runs` path. Those values remain in lifecycle state, where
they enforce self-review rejection but do not pretend to be provider-authenticated
review identities.

The template and validator derive this contract from the same current definition.
Consumers can select a supported override through `.process/standards.json`; see
[generation, verification and custom-format boundaries](ARTIFACT_STANDARDS.md).
`processctl publication validate-pr` checks that selected contract deterministically.
It rejects missing, repeated, misplaced, hidden, unordered, or unsupported visible
structure. Completion checkboxes belong only to the Review and completion section. Ready
pull requests must have every checkbox checked and no unresolved default placeholder
values; drafts may retain pending fields and unchecked work. The author/coordinator
replaces them with actual evidence before ready/merge; the reviewer supplies the verdict.
One trailing `Refs ISSUE.` line remains optional. A ready, contract-identified final
consumer adoption may instead use `Closes ISSUE, closes OWNER/REPOSITORY#NUMBER.` with
the complete keyword/reference syntax repeated for every issue; drafts cannot close
issues. Producer and intermediate pull requests do not close release-source issues.
The managed template never solicits execution identity,
and authors plus independent review keep it out of free-form values. The validator is
a positive grammar for public fields; it deliberately does not guess identities from
an open-ended vocabulary of names or labels.

This producer's local `review` profile runs `verification/verify_publication.py`
against the actual Git branch. `main` is the consumer's integration branch, not a
proposal; detached local checkouts require explicit PR context instead of a guessed
branch name. In CI, the `Adopted public process` job supplies actual PR metadata and
the base/head commit range to the installed publication adapters. Metadata edits and
draft-state changes rerun CI. Maintainers must keep this existing job in `main`'s
required status checks alongside the other required checks. These publication choices
belong to this consumer; the shared lifecycle does not impose a naming policy.

At any point:

    processctl change status --change-id change-123 --json

## Release to consumer PR

Each release includes [reviewed release contents](RELEASE_NOTES.md) generated from
the canonical manifest: one reader-facing record per shipped behavior, its source,
observable result, compatibility impact, and the action needed for that change type.
The manifest retains structured problem, path, application, and notes data for review;
the standard projects only the details that help a reader act, with breaking changes
receiving the fullest impact and adaptation context. The release PR reviews this file;
the GitHub Release publishes the same contents. See [the release procedure](RELEASING.md)
for authoring and checks.

This producer's release identity inputs and text assets declared by
`tool.setuptools.data-files` use UTF-8 without BOM and LF, matching `.gitattributes`.
Writers select that representation explicitly; JSON writers use
`write_json_atomic`. Existing distribution verification checks the declared text
bytes before building. It rejects CRLF, mixed endings, BOM and invalid UTF-8 even
when text-mode reads or Git conversion would hide the difference. Binary data and
fixtures outside that text inventory retain their own format contracts.

Every opted-in consumer uses Renovate's pip-compile manager. Its engineering-process
package rule keeps the adoption pull request in draft and runs exactly:

    python .process/adopt-process.py --project-root . --requirements-lock requirements/process.txt

The bootstrap refreshes `engineering-process` index metadata using pip's
`--refresh-package` option, with `--no-cache-dir` for older pip. Exact pins, hashes,
isolated installation and bounded failures still apply. Consumer CI that installs
the lock directly must also request fresh metadata: use
`--refresh-package engineering-process` with pip 26.2 or newer, or the portable
`--no-cache-dir` option. Keep this setting on the process installation command;
other dependency caches do not need to be disabled. For non-isolated pip-compile,
the equivalent `PIP_REFRESH_PACKAGE=engineering-process` environment setting keeps
fresh release lookup compatible with older pip, which revalidated by default.

postUpgradeTasks.fileFilters includes the managed paths, so Renovate commits the new
hash lock and the fully materialized process in the same pull request. A self-hosted
Renovate administrator must allow only this anchored command and must keep shell
execution disabled:

    ^python \.process/adopt-process\.py --project-root \. --requirements-lock requirements/process\.txt$

The release workflow publishes exact wheel and sdist bytes to PyPI, verifies their
registry hashes, creates the immutable GitHub release, and sends one authenticated
engineering-process-published event to renovate-ops. That control plane runs Renovate
for each repository whose protected config explicitly opts in. Each consumer's normal
CI and independent review decide whether its draft PR can merge; the consumer owner
authorizes that merge.

This repository opts in through .github/renovate.json, so it receives the same
adoption PR as every other consumer. See SELF_HOSTING.md and RELEASING.md.

The optional [Renovate preset](templates/renovate.json) and public template are generated
from the packaged PR standard. It supplies only `prHeader` and `prBodyTemplate`;
dependency selection, supported platforms, schedules, major-update approval,
commands, draft policy, and merge authority remain consumer-owned. Regenerate it
with `python verification/generate_renovate_preset.py`; `--check` rejects drift.
Consumers overriding that standard can generate a matching preset with
`processctl artifact renovate-preset`; wire it through their own Renovate configuration.

Consumers add `github>phuongnse/engineering-process//templates/renovate#COMMIT_SHA`
to their existing `extends` array, replacing `COMMIT_SHA` with the full source
commit of a verified release that contains the preset. Remove obsolete inline
`prHeader` and `prBodyTemplate` overrides, including matching package-rule overrides.
The preset targets the current draft grammar. A consumer adopts a new release by
refreshing the pinned package/hash and regenerating its current template and bot
configuration; the process does not promise that a prior publication contract is
readable by the new release.

Renovate resolves presets from the protected base configuration before dependency
updates and post-upgrade tasks. Bootstrap the current preset in that base before
relying on its generated drafts; changing the candidate configuration alone cannot
repair the same run's body. Verify the native rendered body against both the base
and candidate process authority during adoption. Pending fields and unchecked
Completion gate items are proposals, never evidence or approval.

Before collecting lifecycle evidence for a bot PR, apply its configured
`stopUpdatingLabel` (Renovate's default is `stop-updating`) and confirm the head is
unchanged after any in-flight bot run finishes. Keep the label through verification,
independent review, receipt and the consumer-owned merge. Do not request a native
rebase or dashboard retry while reviewing: an explicit retry can resume updates.
If the candidate changes, open a new implementation cycle and collect fresh
evidence. Remove the pause label only when returning the PR to automation.

These are native [shared preset](https://docs.renovatebot.com/config-presets/) and
[review pause](https://docs.renovatebot.com/configuration-options/#stopupdatinglabel)
boundaries; the control plane does not become a second PR publisher.

## Current contract boundary

Every process-owned artifact, schema, and runtime contract has one current definition
and version `1`. The latest semantics are edited in place, including breaking changes.
The runtime rejects another version or an invalid current shape; it does not normalize
old documents, guess fields, select a compatibility mode, or run a migration adapter.

Consumer adoption is deliberately small: install the released package from the
consumer's exact pinned and hashed requirement, update `.process/project.json` and
integration references to the current shape, regenerate managed artifacts with the
adoption command, recreate any active process artifacts, and rerun required profiles.
Delete or repair unsupported consumer-owned files as part of that change. No process
migration framework or cross-release evidence reuse is provided. Package/release
identity, process digest, requirements hash, snapshot binding, lifecycle correctness,
freshness, and independent review remain authoritative.

## Development

    python -m pip install \
      -r engineering_process/requirements-runtime.txt \
      -r engineering_process/requirements-dev.txt \
      -r engineering_process/requirements-build.txt
    python verification/run_test_suite.py
    python processctl.py skills validate --root process_assets/skills
    python verification/verify_distribution.py

The process repository requires a real consumer incident or request in every process
change contract. PROCESS_IMPROVEMENT.md explains that brake.
