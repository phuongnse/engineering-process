# Engineering Process v3.2.0

Changes since v3.1.0.

## Features

- **Make process guidance easy to find and teach consumer documentation care in ordinary delivery work.** (`documentation-quality (issue #214)`)
  - **What changed:** Make README a reader map, add focused consumer setup, delivery, and documentation-quality routes, align source skills and generated guidance, and verify links and source-derived outputs.
  - **Apply:** Adopt the released process package, regenerate managed skills, instructions, and PR-template outputs, recreate unsupported active process artifacts under the current contract, and rerun the consumer's required profiles. Keep consumer product documentation in its existing organization and update it only when the consumer change affects durable reader knowledge.
  - **Compatibility:** Non-breaking guidance capability within the current version-1 process contracts. The lifecycle and consumer ownership boundaries remain unchanged; the currently adopted 3.1.0 managed surfaces remain authoritative until the consumer adopts the release containing this correction.
- **Make change runtimes resumable, handoffable, and cleanup-safe.** (`runtime-lifecycle-cleanup-handoff (issue #214)`)
  - **What changed:** Adds resumable handoff export/import, validated bounded receipts, retry-safe owner-scoped cleanup, post-cleanup status and PR readers, storage measurement and guarded purge, and lifecycle acceptance coverage.
  - **Apply:** Adopt the updated process assets and run the repository verification profiles; use change handoff export/import for sequential workspace transfer and change storage or change purge --confirm for retention maintenance.
  - **Compatibility:** Non-breaking for current v1 consumers. Existing project and receipt documents remain valid; no distributed service or extra approval or identity gate is introduced. Consumers continue using their owned commands and review policy.

## Fixes

- **Allow optional terminal punctuation in pull request issue references.** ([#239](https://github.com/phuongnse/engineering-process/pull/239))
  - **What changed:** Accept valid Refs and Closes references with or without a terminal period while retaining the complete issue-target grammar, checklist placement, uniqueness, and draft/ready restrictions; align the README, canonical standard, and generated next-distribution template guidance while leaving the currently adopted managed template unchanged until its adoption PR.
  - **Compatibility:** Non-breaking current-v1 compatibility expansion: both existing references with a final period and valid references without one are accepted; incomplete, malformed, duplicated, misplaced, or draft-closing references remain rejected.
- **Make lifecycle evidence and next actions understandable from change status.** ([#214](https://github.com/phuongnse/engineering-process/issues/214))
  - **What changed:** Add a read-only status projection of the current verification selection, candidate and control identities, readiness and review blockers, bounded diagnostics, and one actionable next route while preserving the existing verification report-status field; clarify the direct vocabulary and reader entry point in the delivery guidance.
  - **Compatibility:** Non-breaking current-v1 lifecycle correction: existing lifecycle transitions, verification commands, and the verification report-status field remain authoritative, while consumers can use currentVerification, evidence.requirements, diagnostics, review blockers, and nextAction; no migration layer or automatic retry is provided.

## Upgrade and compatibility

Merge the complete hash-locked package/adoption PR, update the local and CI environments to the selected version, and start a fresh agent session.

Consumer CI, naming conventions and branch-protection settings remain consumer-owned; adoption does not configure them automatically.

No breaking changes are included. Review each item's Compatibility and follow any shown Apply guidance before adopting.

See [versioning and compatibility](https://github.com/phuongnse/engineering-process/blob/v3.2.0/VERSIONING.md) and [adoption guidance](https://github.com/phuongnse/engineering-process/blob/v3.2.0/SELF_HOSTING.md).

[Full change comparison](https://github.com/phuongnse/engineering-process/compare/v3.1.0...v3.2.0)
