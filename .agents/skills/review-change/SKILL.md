---
name: review-change
description: Independently review an immutable verified change against its contract, plan, project policy, diff, and current evidence, then produce structured findings and an approval or changes-requested verdict. Use only from a reviewer actor and isolated context that did not implement the reviewed cycle.
---

# Review a Change

## Goal

Evaluate correctness, security, compatibility, maintainability, and evidence at a
specific checkpoint without changing the reviewed source.

## Workflow

1. Accept the review only in a reviewer actor and isolated context that did not
   implement the current cycle. Use the host's isolated-review mechanism or a
   separate human reviewer; if separation cannot be attested, report blocked.
2. Register the assignment with processctl change review start. Confirm its
   checkpoint, comparison base, contract, plan, and required verification reports
   refer to the same immutable source.
3. Read the diff and only the project-owned contracts needed to evaluate affected
   behavior and trust boundaries.
4. Record actionable findings with severity, exact location, evidence, and status.
   Separate defects from questions, optional improvements, and unsupported claims.
   Resolved, deferred, and false-positive findings require resolution evidence.
5. Request changes when any required finding remains open. Approve only when required
   outcomes and evidence are complete for the reviewed checkpoint.
6. Validate the report with processctl contract validate --kind review, then submit
   it with processctl change review submit. The CLI rejects self-review, stale
   evidence, assignment mismatches, and checkpoint mismatches.
7. Return findings to the implementation owner. Review never silently edits,
   publishes, or expands the accepted scope.

## Hard gates

- Do not approve stale, indirect, missing, or blocked evidence.
- Do not treat review prose as more authoritative than project contracts.
- A reviewer must not intentionally mutate the checkpoint under review.
- The reviewer actor id and context id must both be independent from every
  implementation actor and context recorded for the current cycle.

## Output

Return the structured report, attested reviewer identity, verdict, reviewed
checkpoint and base, unresolved findings, lifecycle phase, and invalidated evidence.
