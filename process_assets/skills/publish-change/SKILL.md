---
name: publish-change
description: Publish and merge a completed, independently reviewed checkpoint through standing project automation policy, then continue authorized release, deployment, adoption, and cleanup operations. Use after lifecycle completion when a valid project policy authorizes automation.
---

# Publish a Change

## Goal

Publish one approved immutable checkpoint through the project-owned remote workflow
without duplicating implementation, verification, or review.

## Workflow

1. Classify the exact authorized remote action and read project publication policy,
   version governance, and current lifecycle status. Do not create a branch or review
   object before the local lifecycle is completed.
2. For source publication, require a current completion record whose checkpoint and
   workspace fingerprint match the source being published. When publication runs on
   a different machine, require a validated external completion receipt and use
   `processctl publication validate-evidence-source`; do not recreate or resubmit the
   semantic review in the publication adapter.
   Also require improvement status to contain no unresolved consumer case; producer
   completion may publish source but cannot claim immutable improvement resolution
   until the separately authorized release exists.
3. Populate the managed review-object sections with project-specific facts. Run
   `processctl publication validate-source` with the change id and exact completion
   commit, then validate branch and commit range. Provider draft/ready status is
   presentation only and never substitutes for completion evidence.
4. Push and create the review object only from the exact completed checkpoint. The
   independent semantic review has already finished; remote CI may validate the same
   receipt/checkpoint but must not fabricate a replacement review.
   When a schema-1/schema-2 controlled automation proposal for a dependency already
   exists under an explicit
   protected-base opt-in, produce fresh exact policy evidence bound to the same head
   and finalized ready metadata, then use `publication validate-proposal-completion`
   with the external receipt. Permit the project adapter to create only the configured
   completion check for that exact head; do not republish source or treat the earlier
   proposal event as completion. A schema-3 Renovate process-adoption proposal never
   uses this completion route: its consumer owner reviews and manually merges the
   complete candidate, and merge is terminal.
5. When a valid standing policy authorizes merge, require its configured method,
   completed lifecycle, exact approved head, current protected base, required checks,
   and branch protection, then enable provider auto-merge or invoke the exact merge.
   A changed head/base invalidates authorization. Do not ask for per-merge confirmation.
   For a release, export and validate the completion receipt, derive the only
   permitted next version from the ordered `release.json` change types, derive every
   identity surface from that contract, and require GitHub tag and title to be the
   exact same `v<SemVer>` before publication.
6. Continue every standing-policy-authorized release, deployment, adoption, and
   ephemeral-cleanup action after merge. Record each remote identifier and terminal
   status. Treat any later source change as a new lifecycle cycle with invalidated
   publication readiness.
7. Involve the owner only for `capability-unavailable`,
   `bounded-recovery-exhausted`, or `decision-required`. A pending provider check or
   bounded retry remains automation work, not an escalation.

## Hard gates

- Never infer automation authority from owner intent alone; the valid standing policy
  must exist on the project boundary before automated publication or merge.
- Do not publish source with stale evidence or unresolved required findings.
- Do not publish through `improvement-required` or `improvement-pending`, and do not
  represent producer completion as release resolution or consumer reproduction.
- Do not create an authoritative review object for a merely planned, implementing,
  verified, review-pending, changes-requested, or approved lifecycle. The only
  pre-completion exception is an explicitly opted-in, policy-validated, untrusted
  automation proposal. A dependency proposal remains merge-blocked until exact-head
  completion; a schema-3 process-adoption proposal remains consumer-owner-merge-only.
- Do not treat static policy verification as semantic independent review.
- Do not merge before the standing policy's completed-lifecycle, review, exact-head,
  current-base, required-check, and method gates all pass.
- Do not hand-edit one release identity surface independently of the release contract
  or update consumer locks before public artifact hashes are verified.
- Do not treat a Renovate proposal as adoption evidence or allow provider automerge
  at any point. A separate agent-host process-authority PR created only after exact
  lifecycle completion may merge automatically under the consumer's standing policy.
- Do not permit the dependency controlled-proposal route to change process authority, workflows,
  release, deployment, security policy, or trust roots, or to enable scripts, plugins,
  shell execution, privileged checks, write-capable proposal checks, or provider
  automerge before exact completion.
- Require one process-adoption PR to contain its compiled hash lock, process lock,
  managed contract, and selected skill snapshots before review. Merge ends adoption;
  never defer synchronization to a post-merge step.
- For schema-3 Renovate process adoption, require a protected-base immutable verifier,
  exact release and source/target authority provenance, exact base/head/path bindings,
  the complete managed distribution and grouped action-pin-only workflow delta. Keep
  `consumerOwnerMergeRequired` true and never let completion, provider state, or
  standing automation escalate it to auto-merge.
- Do not add a reviewer host, daemon, scheduler, generic workflow engine,
  dynamically generated approval chain, meta-assessment, or reviewer-of-reviewer to
  automate this owner boundary.
- Do not replace, omit, or weaken the managed publication sections or standard
  requirements; projects may append stricter metadata and checklists.
- Metadata-only work may skip code implementation only when project policy permits it.

## Output

Return action, change id, checkpoint, completion evidence, metadata validation,
remote state, and blockers.
