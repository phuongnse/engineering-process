# Engineering Process v3.1.0

Changes since v3.0.0.

## Features

- **Add governed recovery for plan and verification blockers.** ([#233](https://github.com/phuongnse/engineering-process/pull/233))
  - **What changed:** Add owner-approved superseding recovery for plan-scope blockers, preserve failed reports with identity-bound diagnostics, distinguish explicit refresh from remaining work after invalidation, and record bounded recovery measurements without weakening lifecycle safeguards.
  - **Apply:** Adopt the v3.1.0 package and exact hash, regenerate managed skills and current project configuration, recreate active process artifacts under the current v1 contract, and rerun required profiles before using the new recovery commands.
  - **Compatibility:** Backward-compatible additive current-v1 lifecycle behavior. Existing contracts and normal six-phase transitions remain valid; consumers must not retry unchanged failed remaining work or bypass owner-controlled recovery and review.
- **Add opt-in bounded verification progress status.** ([#234](https://github.com/phuongnse/engineering-process/pull/234))
  - **What changed:** Add opt-in progress status for verification commands, emitted at a bounded cadence with profile/check position, elapsed time, timeout, byte counts, runner responsiveness, and explicit unknown internal progress while keeping JSON results, timeout, output, cleanup, evidence, and lifecycle semantics unchanged.
  - **Apply:** Adopt the v3.1.0 package and exact hash, regenerate managed verification guidance and current configuration, recreate active process artifacts, and rerun required profiles; enable --progress only when bounded status is useful.
  - **Compatibility:** Backward-compatible additive current-v1 CLI behavior. Existing callers without --progress remain unchanged, progress is not lifecycle evidence, and consumers retain their own command and output policies.

## Fixes

- **Bind delivery evidence to the exact candidate and provider state.** ([#235](https://github.com/phuongnse/engineering-process/pull/235))
  - **What changed:** Record candidate-bound publication failures and make metadata-only events retain code-check evidence only through provider-bound workflow and artifact relationships, with exact base/head freshness and ordered release-PR metadata refresh.
  - **Compatibility:** Non-breaking correction to the current delivery and publication evidence boundary. Existing release and adoption authorities remain in place, while stale or incomplete metadata evidence is rejected instead of treated as a pass.
- **Tighten evidence reuse and reader-facing process outputs.** ([#232](https://github.com/phuongnse/engineering-process/pull/232))
  - **What changed:** Tighten evidence and incident boundaries, align current version-1 artifact standards and renderers, and streamline reader-facing process outputs while retaining lifecycle, adoption, and publication guarantees.
  - **Compatibility:** Non-breaking correction within the current version-1 process contract. Consumers must refresh process-owned generated outputs at the adoption boundary; no legacy reader, migration adapter, or cross-release artifact reuse is promised.

## Upgrade and compatibility

Merge the complete hash-locked package/adoption PR, update the local and CI environments to the selected version, and start a fresh agent session.

Consumer CI, naming conventions and branch-protection settings remain consumer-owned; adoption does not configure them automatically.

No breaking changes are included. Review each item's Compatibility and follow any shown Apply guidance before adopting.

See [versioning and compatibility](https://github.com/phuongnse/engineering-process/blob/v3.1.0/VERSIONING.md) and [adoption guidance](https://github.com/phuongnse/engineering-process/blob/v3.1.0/SELF_HOSTING.md).

[Full change comparison](https://github.com/phuongnse/engineering-process/compare/v3.0.0...v3.1.0)
