# Portable Execution Contract

Apply these semantics throughout every change lifecycle.

## Universal gates

1. Read the full active skill, this reference, the nearest AGENTS.md, and the affected
   project owners before editing.
2. Follow phase order. A stated stop condition blocks dependent work.
3. Defer a specific accepted outcome only with explicit approval and an owner. A skip
   never waives dependent gates implicitly.
4. Reuse evidence only while its artifact, checkpoint, workspace fingerprint,
   command, environment, and acceptance boundary remain current.
5. Remove superseded implementation and guidance when compatibility is not required.
   Do not preserve a retired path as an undocumented safety default.
6. Separate development completion from commit creation, publication, merge, release,
   deployment, and destructive data operations.

## Change-driven scope

Map affected paths, callers, consumers, trust boundaries, migrations, generated
artifacts, documentation, and evidence-required dependencies to complete work items.
Run the smallest profile that proves each accepted outcome. Use a broader profile only
when cross-cutting invalidation, inseparable dependencies, or project policy requires
it. Do not infer broad completion from a focused check.

## Engineering method

1. Trace the governing contract and real flow before choosing an owner or design.
2. Prefer no change, existing code, the standard library, native platform behavior,
   and installed dependencies before custom mechanisms, while preserving required
   safety and acceptance behavior.
3. For a defect, prove the smallest reliable failure first, state one hypothesis, test
   one variable, implement the root-cause fix, and prove the behavior afterward.
4. Treat a proposed path as a workaround when it changes the required owner, runtime,
   authority, trust boundary, invariant, or evidence boundary merely to keep moving.
   Return to specification and planning instead.
5. Keep one writer for overlapping source. Delegate bounded disjoint work only when
   the host supports it and the handoff preserves exact scope, permissions, stop
   conditions, and evidence ownership.

## Blocker protocol

When progress depends on user-controlled or external state:

1. Classify repository defect, missing product decision, or external-state blocker.
2. Reproduce through the smallest permitted boundary and preserve the exact command,
   exit status, error, environment, and missing authority.
3. Continue safe read-only diagnosis, but stop mutation at authentication, consent,
   permission, host setup, destructive action, or approval boundaries.
4. Do not substitute a different command, library, runtime, environment, proxy,
   credential path, disabled control, or indirect API as evidence for the required
   boundary.
5. Report `Blocker`, `Evidence`, `Boundary`, `User action or decision needed`, and
   `Safe next step after confirmation`.

## Independent review

Review begins only after all baseline and change-required profiles pass on one clean
immutable checkpoint. The reviewer must be a read-only actor and context unused by
the current implementation cycle. The agent host or human organization attests that
identity separation; processctl validates the attestation structure and rejects
identity reuse or stale evidence.

A running or pending reviewer means review pending, not failure or approval. The
reviewer reads the assignment, diff, contracts, plan, and existing evidence; it runs
only a focused reproducer for a concrete finding or evidence gap. It never edits
tracked source or Git state. Any source mutation invalidates the assignment.

An open required finding produces changes-requested. Preserve its checkpoint and
evidence, classify the finding against the owning contract, implement the smallest
correct resolution in a new cycle, and repeat invalidated verification and review.

## Completion audit

Map every acceptance criterion to current source and required verification. Require
an approved independent review for the exact same checkpoint and workspace
fingerprint, with no open required finding. Missing, stale, indirect, or blocked
evidence remains incomplete. processctl completion is an engineering result, not
publication or release authorization.

## Process improvement

Classify a validated defect or finding as local behavior, reusable process semantics,
deterministic enforcement, portability gap, or obsolete guidance. Fix the smallest
correct owner, add regression proof for deterministic behavior, and remove duplicate
or superseded rules. Do not memorialize an incident as ceremony without evidence of
the reusable class.
