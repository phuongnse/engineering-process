# Repository governance

Repository governance is the external integration gate between an approved source
checkpoint and the default branch. It supplements the engineering lifecycle; it does
not replace verification, independent review, merge authorization, or release
authorization.

## Portable baseline

Every governed repository declares `.process/repository-governance.json`. New policies
use schema 2, which requires the default branch to:

- accept changes only through a governed review object;
- block deletion and history rewrites;
- forbid bypass actors;
- require the stable `Change metadata policy` and `Merge eligibility` contexts;
- declare whether required checks must be refreshed after the branch falls behind the
  default branch.

Released schema 1 remains readable with its `blockNonFastForward` field and the same
history-protection meaning. Migration to schema 2 changes only that representation to
the provider-neutral `blockHistoryRewrite`; it does not weaken or reapply live policy.

Projects may add stronger checks or a separate stronger protection policy. They
cannot remove or rename the two stable contexts. `Change metadata policy` validates
current review metadata and publication policy. `Merge eligibility` succeeds only
when all project-owned verification for the immutable head checkpoint succeeds. Individual
matrix and domain job names remain project-owned implementation details behind
`Merge eligibility`; copying all of them into remote settings creates avoidable
configuration drift.

`Change metadata policy` also binds each satisfied publication claim to the
referenceable evidence required by the publication contract. A structural status
without its published contract, verification, or independent-review evidence fails
before integration. Projects retain access-control, retention, and immutability
ownership for referenced artifacts.

A protected branch prevents integrating a checkpoint whose required outcomes fail or
are missing. It cannot make flaky verification reliable, and it cannot prove that a
later rerun will have the same result. Reliability defects still require their own
reproducer, fix, and regression evidence.

## Integration identity contract

Integration metadata is an observability surface, not decoration. Every adapter
exposes stable machine identities for required contexts and meaningful human-facing
names for the operations that produce them. Provider-native workflow, job, step,
artifact, and environment naming belongs to the adapter contract. Required context
identities are public policy and change only through a governed migration.

## Adapter boundary

A provider adapter translates the portable policy into one hosted system without
changing its meaning. It owns authentication, permission discovery, normalized live
state, provider resource selection, compare-before-write planning, mutation requests,
and read-back verification. Adding another provider creates a peer adapter; it does
not add provider branches to this baseline.

Read-only checks fail when the provider cannot disclose enough state to prove the
baseline. Mutations require a separately authorized plan bound to the current
repository identity, policy, live state, review head, and required-check evidence.
Immediately before mutation, the adapter re-reads every bound input; any drift fails
closed. An adapter never deletes protection, creates bypass authority, or mutates live
settings implicitly during another lifecycle phase.

Concrete provider commands, credential sources, permission mappings, resource shapes,
and automation examples belong to that adapter's distributed guide.

## Verification ownership

The consumer owns domain verification and its execution topology. The integration
exposes two stable public outcomes:

1. `Change metadata policy` evaluates the current review object's publication
   metadata whenever that metadata or source identity changes.
2. `Merge eligibility` evaluates the complete required verification result for the
   same immutable source. It always reports a final result even when upstream work
   fails or is intentionally omitted. An omission is acceptable only when the
   project-owned impact contract proves the work is inapplicable.

Keeping these outcomes separate allows metadata changes to refresh their own evidence
without repeating unrelated domain verification. A source change invalidates both
outcomes and requires current evidence for the new identity.

## Rollout order

Repository protection is intentionally not self-activating:

1. Add the policy and stable outcome producers through the repository's existing
   authorized integration path.
2. Observe successful `Change metadata policy` and `Merge eligibility` checks on one
   exact review head.
3. Run read-only policy validation and create a plan bound to that evidence.
4. Obtain separate repository-owner authorization for the external settings write.
5. Apply the current plan, read provider protection back, and require the check to
   pass.
6. Only then rely on the provider protection as an integration or release prerequisite.

The producer follows the same order. Source under development can add and verify the
contract and adapter, but it does not use itself as authority to mutate the hosted
system. The current immutable public authority—or an owner applying the reviewed
exact settings during a bootstrap window—owns activation.

An existing consumer with provider-native required contexts migrates only after
adopting the immutable process release. It adds the two stable outcomes, maps every
project-owned verification result to `Merge eligibility`, observes both outcomes on
one exact review head, and then plans an update of its uniquely owned protection
resource. The consumer owns which dependency results may be omitted; each exception
must derive from successful impact selection. Consumer source and settings never
depend on an uncommitted producer checkout.
