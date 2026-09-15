# Engineering Process v2.6.0

Changes since v2.5.0.

## Features

- **Allow explicitly proven impact units to satisfy final verification.** ([#205](https://github.com/phuongnse/engineering-process/issues/205))
  - **Problem:** Impact selection could provide fast feedback but could not satisfy a final required profile, so consumers with complete explicit coverage still had to launch a monolithic final suite.
  - **What changed:** Add a schema-version 2 finalProfiles opt-in. For opted required profiles, complete changed-path resolution and an explicit global unit permit selected impact units to run as final impact-assurance evidence. Policy or candidate changes, missing coverage and unresolved paths fail closed; schema-version 1 policies remain feedback-only.
  - **Where:** `schemas/project.schema.json`, `schemas/verification-impact-selection.schema.json`, `schemas/verification-selection.schema.json`, `schemas/run.schema.json`, `schemas/receipt.schema.json`, `engineering_process/project.py`, `engineering_process/impact.py`, `engineering_process/lifecycle.py`, `engineering_process/cli.py`, `process_assets/skills/change-plan/SKILL.md`, `process_assets/skills/change-verify/SKILL.md`, `SELF_HOSTING.md`, `README.md`, `tests/test_contracts.py`, `tests/test_impact.py`, `tests/test_lifecycle.py`, `tests/test_skills.py`
  - **Apply:** Adopt the package release, then opt selected required profiles into impactProfiles schema version 2 with finalProfiles only after reviewing complete path coverage and global cross-cutting units. Run change verify --remaining; use explicit change verify --profile when a full refresh is required.
  - **Compatibility:** Backward-compatible opt-in. Existing schema-version 1 consumers and explicit full-profile callers retain their behavior. Final impact assurance is unavailable until the consumer policy is deliberately migrated and reviewed.
  - **Notes:** Final impact assurance records execution mode and selection identity in the existing lifecycle evidence; it does not create a second lifecycle or permit inferred coverage from filenames, labels, commands, or diagnostics.
- **Select verification units from explicit change impact without fallback.** ([#205](https://github.com/phuongnse/engineering-process/issues/205))
  - **Problem:** A consumer could only choose a whole profile or reuse a whole-profile result, so a small change lacked a standard way to run related verification units. When impact was unclear, an automatic full-suite fallback could hide that dependency reach had not been resolved.
  - **What changed:** Add versioned consumer-owned impactProfiles, deterministic changed-path resolution, change explain --impact, and change verify --affected. Every path must resolve to matching units or the command stops with an explicit re-analysis action; an explicit global pattern is the only broad selection rule. Affected runs use the bounded runner and remain feedback evidence, separate from final assurance evidence.
  - **Where:** `schemas/project.schema.json`, `schemas/verification-impact-selection.schema.json`, `engineering_process/impact.py`, `engineering_process/repository.py`, `engineering_process/lifecycle.py`, `engineering_process/cli.py`, `process_assets/skills/change-verify/SKILL.md`, `SELF_HOSTING.md`, `README.md`
  - **Apply:** Adopt the package release, add impactProfiles to the consumer project policy, map every changed path to one or more independently executable commands, and run change explain --impact before change verify --affected. Resolve every unresolved path by inspecting the diff/dependency reach and updating the consumer-owned mapping or recording an owner decision.
  - **Compatibility:** Backward-compatible capability. Existing project files, explicit profile verification, whole-profile reuse, required lifecycle profiles, and final review/release evidence remain supported. Consumers without impactProfiles receive a clear unavailable result.
  - **Notes:** Path patterns are a versioned consumer policy, not a process guess. Overlapping units all run; an explicit global unit may intentionally subsume narrower units after its trigger matches. Affected feedback does not advance the lifecycle or prove final assurance.

## Fixes

- **Render release records as readable Markdown without gratuitous escapes.** ([#171](https://github.com/phuongnse/engineering-process/issues/171))
  - **Problem:** Generated release notes escaped every punctuation mark and padded ordinary code references, making otherwise complete issue-level records difficult to read.
  - **What changed:** Escape only Markdown and HTML-sensitive metadata syntax, keep ordinary punctuation readable, and render simple owned references as normal inline code while retaining safe fencing for embedded backticks.
  - **Where:** `engineering_process/release_notes.py`, `tests/test_release.py`, `release-changes/README.md`, `RELEASING.md`, `ARTIFACT_STANDARDS.md`, `RELEASE_NOTES.md`
  - **Apply:** Use the normal prepare-release workflow and review the generated RELEASE\_NOTES.md against release.json before publication.
  - **Compatibility:** No breaking change. Schema-v5 manifests and legacy release-note data remain readable; the selected release-notes@1 structure and published v2.5.0 body are not rewritten.
  - **Notes:** This correction applies to subsequently generated release bodies. Published release artifacts remain immutable and must be corrected only through the owner's release policy.
- **Batch reusable verification evidence across lifecycle stages.** ([#198](https://github.com/phuongnse/engineering-process/issues/198))
  - **Problem:** Continuation verification could make repeated reuse decisions and authority calculations one profile at a time even when several reports already covered the same unchanged candidate.
  - **What changed:** Batch valid whole-profile reuse decisions into one canonical state write and reuse one process-authority digest across stable evidence comparisons, while retaining fresh candidate snapshots, runtime identity checks, explicit refresh semantics, and correction-cycle invalidation.
  - **Where:** `engineering_process/evidence.py`, `engineering_process/lifecycle.py`, `engineering_process/pr_description.py`, `process_assets/skills/change-verify/SKILL.md`, `README.md`, `tests/test_lifecycle.py`, `tests/test_skills.py`
  - **Apply:** Use change verify --remaining for continuation work. The process reuses only exact valid reports and records reuse without relaunching their child commands.
  - **Compatibility:** No breaking change. Explicit profile verification remains an unconditional refresh; stale, legacy, or mismatched evidence reruns; final review and finish still require exact-snapshot evidence.
  - **Notes:** The optimization is operation-scoped and does not add a persistent success cache or reuse evidence across a changed candidate, mutation boundary, or incompatible runtime.
- **Make the full verification suite deterministic and expose bounded timing diagnostics.** ([#201](https://github.com/phuongnse/engineering-process/issues/201))
  - **Problem:** The repository-owned full verification suite took about six minutes on Windows and review-context tests incorrectly reported stale evidence when run after the complete suite, while local output did not identify the slowest tests.
  - **What changed:** Give review-context tests an isolated fixture owner, seed their verified states once per test class, use the same bounded child environment as the verification boundary, and report aggregate, slow-test, fixture-setup, and profile timing without changing profile exit semantics or deleting behavioral coverage.
  - **Where:** `tests/test_review_contexts.py`, `verification/run_test_suite.py`
  - **Apply:** Run the normal development and review profiles; use the bounded suite timing and slow-test summary to diagnose future cost, and do not replace final verification with a focused subset.
  - **Compatibility:** No breaking change. Existing profile commands, required checks, skip behavior, process/Git/filesystem boundaries, and stored evidence semantics remain supported.
  - **Notes:** The reviewed change evidence records repeated comparable self-consumer measurements and the bounded improvement method; it does not establish a universal latency target. Shared mutable checkouts and blanket mocks remain out of scope.

## Upgrade and compatibility

Merge the complete hash-locked package/adoption PR, update the local and CI environments to the selected version, and start a fresh agent session.

Consumer CI, naming conventions and branch-protection settings remain consumer-owned; adoption does not configure them automatically.

No breaking changes are included. Read each change's Apply, Compatibility and Notes entry before adopting.

See [versioning and compatibility](https://github.com/phuongnse/engineering-process/blob/v2.6.0/VERSIONING.md) and [adoption guidance](https://github.com/phuongnse/engineering-process/blob/v2.6.0/SELF_HOSTING.md).

[Full change comparison](https://github.com/phuongnse/engineering-process/compare/v2.5.0...v2.6.0)
