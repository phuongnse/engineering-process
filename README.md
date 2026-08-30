# Engineering Process

Engineering Process is a small, agent-neutral way to drive repository changes through
one auditable path:

    start → plan → implement → verify → independent review → finish

Portable skills explain what to do. processctl owns state transitions and current
evidence. Each consumer owns its product rules, exact commands, merge policy, and
release decisions.

## Architecture

The distribution has four live parts:

1. Eight skills under process_assets/skills, all reachable from run-change.
2. One processctl state machine under engineering_process/lifecycle.py.
3. Eleven JSON Schemas that are loaded directly by the runtime.
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

A consumer declares `.process/readiness.json` with a production target and maps each
required capability to one or more project-required profiles. A known pack fails
closed when a capability is absent, duplicated, references an unknown profile, or
relies only on an optional profile:

    {
      "target": "production",
      "packs": ["library-cli"],
      "capabilities": [
        {"id": "correctness", "evidenceProfiles": ["development"]},
        {"id": "runtime-safety", "evidenceProfiles": ["development"]},
        {"id": "compatibility", "evidenceProfiles": ["development"]},
        {"id": "portability", "evidenceProfiles": ["development", "review"]},
        {"id": "installability", "evidenceProfiles": ["review"]},
        {"id": "distribution-integrity", "evidenceProfiles": ["review"]},
        {"id": "adoption-integrity", "evidenceProfiles": ["development", "review"]}
      ]
    }

`project validate` and `doctor` resolve that declaration to the exact checks owned by
the consumer. The declaration does not make a weak command sufficient: normal CI and
independent review still judge whether those commands prove the named capability.

The sidecar is a deliberate self-hosting boundary. Public authority N continues to
validate the unchanged strict `.process/project.json` while source N+1 validates and
self-applies the new readiness contract. Adoption leaves the consumer-owned sidecar
in place, so every later authority can repeat the same forward-compatible sequence.

The first pack is intentionally only `library-cli`, derived from this repository as a
real producer and self-consumer. The approved `operations` and `desktop`/`frontend`
packs will be extracted while applying readiness to renovate-ops and LyricRail; they
are not specified in advance. Consumers without readiness remain compatible during
that evidence-backed rollout.

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
      "schemaVersion": 4,
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
      "risks": []
    }

    {
      "schemaVersion": 5,
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
      "findings": []
    }

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
