# Engineering Process

Engineering Process is a small, agent-neutral way to drive repository changes through
one auditable path:

    start → plan → implement → verify → independent review → finish

Portable skills explain what to do. processctl owns state transitions and current
evidence. Each consumer owns its product rules, exact commands, merge policy, and
release decisions.

## Architecture

The distribution has four live parts:

1. Managed skills under process_assets/skills, all reachable from run-change.
2. One processctl state machine under engineering_process/lifecycle.py.
3. JSON Schemas that are loaded directly by the runtime.
4. One adoption transaction that synchronizes managed skills and configuration from
   an exact hash-locked package.

Lifecycle state is local under .process/runs and completion creates one bounded receipt
under .process/receipts. Both paths are ignored by Git. Verification evidence is bound
to HEAD plus a fingerprint of tracked and non-ignored untracked files. Any relevant
mutation invalidates it.

Independent review is one direct rule: neither the reviewer actor nor reviewer context
may have implemented the current cycle. There is no attestation hierarchy,
recommendation chain, authority-transition protocol, remote-evidence federation, or
second handwritten validator.

Runtime architecture is enforced by semantic fitness functions, not module or source-
line quotas. Every module has an explicit dependency layer, imports point toward lower
layers, the internal graph remains acyclic, and lifecycle.py alone owns state
transitions behind the CLI adapter. Size metrics may guide refactoring but do not
decide correctness or release eligibility.

## Consumer configuration

Python 3.11 or newer and Git are required. A consumer owns .process/project.json:

    {
      "schemaVersion": 5,
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
      }
    }

Commands are argument arrays, never shell strings. Each command has a finite timeout.
Output has a hard aggregate budget; evidence stores byte counts and hashes, never raw
stdout or stderr that could contain secrets.

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
validate the unchanged strict `.process/project.json` while source N+1 validates and
self-applies the new readiness contract. Adoption leaves the consumer-owned sidecar
in place, so every later authority can repeat the same forward-compatible sequence.
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
Consumers without readiness remain compatible during that evidence-backed rollout.

### Production engineering invariants

Every new plan and independent review applies one small, versioned invariant floor:

- authoritative structure for open-world decisions;
- one authoritative source for shared policy;
- bounded, least-authority side effects;
- explicit compatibility and migration boundaries;
- assurance bound to current objective evidence and independent judgment.

The canonical triggers, required structures, prohibited failures, and expected
evidence live once in the managed `production-engineering/invariants.json` asset.
They are cross-domain invariants, not a catalog of preferred design patterns. A
closed, owner-versioned protocol may use literal state or enum tables; automation
must not guess open-world meaning from keywords, identifiers, filenames, diagnostic
text, or growing exception lists.

Plan schema version 5 requires a reasoned applicability decision for every invariant
and real work-item references for each applicable entry. A change started by this
authority records that writer requirement before planning. The public reader retains
schema version 4 so a run started by an earlier 1.x authority can still validate and
register its old plan; that registration is assigned review schema version 6. Review
schema version 7 requires an independent result and evidence for each entry. A
violation links to a blocking finding, so it cannot coexist with approval. Structural
completeness is machine-enforced; the reviewer remains responsible for contextual
truth.

This assessment is not a production certificate. Production still requires the
consumer's immutable readiness pack, every required capability in `enforced` state,
fresh consumer-owned verification on the exact candidate, and independent review.

For each ordinary change, `run-change` first surfaces this readiness view. The accepted
request and consumer rules determine which capabilities are affected. Every change
retains the project's baseline `requiredProfiles`; start and plan add any conditional
evidence profiles needed by affected capabilities and include a planned gap only when
the accepted request explicitly selects it. Implement and review protect the enforced
floor. A planned-to-enforced promotion is a reviewed consumer source diff with fresh
evidence. Unrelated planned gaps remain visible but do not block development, and no
skill chooses product priorities or changes readiness automatically.

When a consumer incident exposes a reusable process gap, `improve-process` first keeps
the consumer safe, then prepares a sanitized GitHub issue draft from that checkout.
It deduplicates by consumer/process-version/invariant, requires owner authorization
before `gh issue create`, and uses an accepted issue as the later process change source
and `consumerEvidence`. No producer clone, consumer-CI write token, automatic process
mutation, or wait for a process release is required to continue consumer development.

Pin the process in requirements/process.in:

    --only-binary :all:
    engineering-process==1.0.1

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
- a schema migration of .process/project.json

It removes obsolete skills named by the previous process lock and preserves
consumer-owned skills and instructions. Applying the same version twice is a no-op.
The legacy managed runner can enter 1.0 directly, so consumers do not need a chain of
per-version migration documents. The same transaction deletes the retired migration
directory and standing automation policy; the Windows Job Object helper remains a
managed runtime-containment asset.

The three authored lifecycle documents stay intentionally small. A change contains
source, scope, outcomes, and profiles; a plan binds its digest; a review binds the
assigned checkpoint:

    {
      "schemaVersion": 5,
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
      "schemaVersion": 5,
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
      "schemaVersion": 7,
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

`change review start` returns the exact `reportSchemaVersion` for its assignment and
bounded `processSignals` derived from existing lifecycle events. Signals are prompts
for independent judgment, not evidence that hidden external actions occurred.
Version 6 distinguishes priority from severity: priority records impact if unresolved,
while severity alone controls the current lifecycle gate. Every non-blocking finding
in versions 6 and 7 records one disposition: `resolved` with a rationale, or
`accepted-risk` / `tracked-follow-up` with a rationale, owner, and stable HTTPS
`recordUrl`. Version 7 adds the production-engineering resolution and requires a
`processImprovement` classification of `none`, `consumer-specific`, or
`shared-process`. A shared-process report requires an existing, owner-authorized issue
URL; without it, the review remains pending. Earlier plan and review documents remain
readable, and their runs remain registrable or finishable with the version selected by
the authority that started the relevant phase.

The [finding priority definitions](process_assets/skills/review-change/SKILL.md#finding-priority)
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

### Public pull-request evidence

The managed pull-request template keeps public assurance separate from local
lifecycle identity. Its five sections and labeled fields are ordered and stable:
outcome and scope; source, risk, compatibility, and stack; profiles, snapshot, and
completion receipt; verdict, cycles, blocking status, and non-blocking dispositions;
and a distinct completion gate for the overall assertions.
The public description never needs an actor ID, context ID, reviewer
handle, or local `.process/runs` path. Those values remain in lifecycle state, where
they enforce self-review rejection but do not pretend to be provider-authenticated
review identities.

`processctl publication validate-pr` checks that public contract deterministically.
It rejects missing, repeated, misplaced, hidden, unordered, or unsupported visible
structure. Completion checkboxes belong only to the Completion gate section. Ready
pull requests must have every checkbox checked; drafts may retain unchecked work.
One trailing `Refs ISSUE.` line remains optional. A ready, contract-identified final
consumer adoption may instead use `Closes ISSUE, closes OWNER/REPOSITORY#NUMBER.` with
the complete keyword/reference syntax repeated for every issue; drafts cannot close
issues. Producer and intermediate pull requests do not close release-source issues.
The managed template never solicits execution identity,
and authors plus independent review keep it out of free-form values. The validator is
a positive grammar for public fields; it deliberately does not guess identities from
an open-ended vocabulary of names or labels.

At any point:

    processctl change status --change-id change-123 --json

## Release to consumer PR

Every opted-in consumer uses Renovate's pip-compile manager. Its engineering-process
package rule keeps the adoption pull request in draft and runs exactly:

    python .process/adopt-process.py --project-root . --requirements-lock requirements/process.txt

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

## Compatibility

Version 1.x retains a few small pre-1.0 command shapes so existing consumers can
pass their first adoption PR:

- setup runs only the consumer-owned setup arrays migrated from its old manifest;
- doctor --profile validates the selected profile.
- publication validate-branch, validate-commit, validate-range, and validate-pr remain
  read-only while consumers move those conventions into their own repositories.

They do not restore the removed governance machinery and can be deleted in the next
package major after all known consumers have adopted 1.x.

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
