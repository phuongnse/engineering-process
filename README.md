# Engineering Process

Engineering Process is a small, agent-neutral way to move a repository change
through one auditable path:

    start → plan → implement → verify → independent review → finish

It owns lifecycle transitions, portable guidance, schemas, and managed adoption
assets. The consumer owns product behavior, document organization, exact commands,
merge policy, deployment, and release decisions.

## Start with the route that matches your work

| Reader or task | Read first | Then |
| --- | --- | --- |
| New consumer | [Consumer setup](docs/consumer-setup.md) | [Self-hosting and adoption](SELF_HOSTING.md) |
| Working on a change | [Delivery guide](docs/delivery.md) | [deliver-change](process_assets/skills/deliver-change/SKILL.md) |
| Deciding what documentation to change | [Documentation quality](docs/documentation.md) | The phase skill for the current lifecycle state |
| Reviewing historical measurements | [Performance observations](docs/performance.md) | Treat them as bounded history, not a target |
| Changing Engineering Process itself | [Process improvement](PROCESS_IMPROVEMENT.md) | [Versioning](VERSIONING.md) and [Releasing](RELEASING.md) |
| Using generated issue, PR, or release documents | [Artifact standards](ARTIFACT_STANDARDS.md) | The consumer's selected standard and existing profile |

The README is the map. The linked pages are the references for their own subject;
they are not parallel definitions of the lifecycle.

## What a consumer does first

1. Install a supported Python runtime and the pinned process package.
2. Keep the consumer's exact commands in .process/project.json.
3. Validate the project and readiness declaration.
4. Start every non-trivial change through deliver-change; it selects the only
   valid phase from current lifecycle state.

From a configured consumer checkout, the first useful read-only checks are:

    processctl project validate --json
    processctl change status --change-id CHANGE_ID --json

The [consumer setup guide](docs/consumer-setup.md) shows the smallest configuration.
The [delivery guide](docs/delivery.md) shows the complete command route.

## The lifecycle

deliver-change is the entry point. It routes exactly one phase:

    change-start → change-plan → change-implement → change-verify
      → change-review → change-complete

process-improve handles a reusable process problem before returning to the same
lifecycle. production-engineering supplies the small invariant floor used while
planning and reviewing. Neither specialization creates another lifecycle.

Each phase has one job:

- start accepts a bounded request with real consumer evidence;
- plan binds the approach, literal affected paths, risks, and evidence;
- implement changes only the accepted boundary;
- verify runs the consumer's required profiles on one unchanged candidate;
- review is independent and judges the complete diff, evidence, and reader impact;
- finish records the result; release, merge, adoption, and deployment stay with the owner.

The phase skills are the operational source for those jobs. The process graph and
processctl are the state authority; prose cannot advance or replace them.

## Ownership and source boundaries

| Information | Authoritative home |
| --- | --- |
| Lifecycle state and evidence freshness | processctl and the packaged JSON schemas |
| What an agent does in a phase | The matching skill under process_assets/skills |
| Consumer product behavior and documentation | The consumer repository |
| Process artifact format | The selected artifact standard and its adapter |
| Generated template or preset | Its packaged source standard and generator |
| Current versus adopted release behavior | [Versioning](VERSIONING.md) and [Self-hosting](SELF_HOSTING.md) |
| Historical shipped behavior | [Release notes](RELEASE_NOTES.md) and the release manifest |
| Historical performance measurements | [Performance observations](docs/performance.md) |

Do not maintain a second definition just to make a page look complete. Summaries
should link to the authority they explain.

## Guarantees that matter

- Required profiles are consumer-owned argument arrays with bounded execution.
- Evidence is bound to the accepted contract, plan, candidate, process authority,
  runtime, and input identity.
- A changed candidate cannot reuse a stale verification or review.
- A reviewer actor and context cannot have implemented the current cycle.
- Managed adoption is hash-locked and preserves consumer-owned files.
- A finished change retains the minimum result needed by readers after runtime cleanup.

These guarantees do not decide product behavior, merge permission, deployment, or
release timing.

## Development

Use the repository virtual environment explicitly:

    # POSIX
    .venv/bin/python verification/run_test_suite.py
    .venv/bin/python processctl.py skills validate --root process_assets/skills
    .venv/bin/python processctl.py release validate
    .venv/bin/python verification/verify_distribution.py

Use .venv\Scripts\python.exe on Windows. Commands in consumer configuration are
argument arrays, never shell strings. Read [AGENTS.md](AGENTS.md) for repository
rules and the complete verification commands.

## Keep reading

- [Documentation quality](docs/documentation.md) explains how documentation impact
  belongs in ordinary delivery work.
- [Delivery](docs/delivery.md) explains state, evidence, recovery, and independent
  review without requiring a reader to inspect runtime files.
- [Consumer setup](docs/consumer-setup.md) explains installation, configuration,
  readiness, and adoption boundaries.
- [Artifact standards](ARTIFACT_STANDARDS.md) explains selected PR, issue, release,
  and automation-name documents.
- [Process improvement](PROCESS_IMPROVEMENT.md) explains the real-consumer evidence
  boundary for changing this distribution.
- [Self-hosting](SELF_HOSTING.md), [Versioning](VERSIONING.md), and
  [Releasing](RELEASING.md) cover adoption and publication.
