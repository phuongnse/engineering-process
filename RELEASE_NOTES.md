# Engineering Process v2.7.0

Changes since v2.6.0.

## Features

- **Automate bounded process-improvement incident intake at change finish.** ([#214](https://github.com/phuongnse/engineering-process/issues/214))
  - **Problem:** Process-improvement incidents were detected and filed through manual triage or post-hoc inspection, creating reliance on human intervention and unbounded or unrecorded incident intake.
  - **What changed:** Automate closed-taxonomy incident intake at lifecycle finish before completion receipt sealing, deduplicate against the open tracker, log verification invalidations, sanitize execution identity, and enforce bounded taxonomy and recursion limits.
  - **Where:** `engineering_process/evidence.py`, `engineering_process/incidents.py`, `engineering_process/lifecycle.py`, `process_assets/skills/change-complete/SKILL.md`, `process_assets/skills/process-improve/SKILL.md`, `tests/test_architecture.py`, `tests/test_incidents.py`, `tests/test_lifecycle.py`
  - **Apply:** Adopt the new release package; change finish automatically runs closed-taxonomy incident intake and deduplication without requiring consumer configuration changes.
  - **Compatibility:** No breaking change. Incident taxonomy is closed and governed by the repository owner; deduplication against tracker prevents recursion, and normal lifecycle transitions remain unchanged.
  - **Notes:** Incident intake runs only before sealing completion receipts. Invalidation logs are recorded during verification; intake ignores invalidations if already deduplicated or outside the closed taxonomy.

## Fixes

- **Decouple ambient host environment from execution identity with Zero-List architecture.** (`owned change #agent-neutral-runtime-identity`)
  - **Problem:** Runtime evidence identity hashed arbitrary ambient host environment variables, requiring fragile blacklist/whitelist workarounds and breaking verification evidence reuse when session metadata changed.
  - **What changed:** Adopt a Zero-List architecture: decouple ambient host environment from execution identity digest, eliminate all transient keyword lists, add an automated architecture fitness test forbidding AI vendor couplings, and enforce anti-workaround review rules.
  - **Where:** `engineering_process/evidence.py`, `process_assets/skills/change-plan/SKILL.md`, `process_assets/skills/change-review/SKILL.md`, `tests/test_architecture.py`, `tests/test_commands.py`
  - **Apply:** No configuration changes required; runtime identity deterministically hashes only process-controlled interpreter, dependencies, and platform facts.
  - **Compatibility:** No breaking change. Child execution environments continue to inherit OS tools and paths with secrets sanitized.
  - **Notes:** Eliminates all keyword lists and workarounds. Runtime identity is invariant to external agent harnesses and host terminal session state.
- **Enforce structural architecture verification over keyword test assertions and generalize invariant guidance.** (`owned change #structural-architecture-verification`)
  - **Problem:** Architecture tests verified runtime invariants by searching for specific variable and token names, approximating structural invariants with keyword string matching and failing to detect architectural deviations under different identifiers.
  - **What changed:** Replace keyword-based token assertions in architecture tests with AST syntax boundary inspection, generalize the authoritative-structure invariant to require structural syntax or behavioral invariance, and mandate independent reviewers to reject keyword/identifier name assertions in tests.
  - **Where:** `process_assets/skills/change-review/SKILL.md`, `process_assets/skills/production-engineering/SKILL.md`, `process_assets/skills/production-engineering/invariants.json`, `tests/test_architecture.py`
  - **Apply:** No action required; architecture tests and process guidance apply immediately.
  - **Compatibility:** Non-breaking. Preserves test suite pass status while establishing structural AST verification.
  - **Notes:** Ensures architectural verification tests invariant structural properties or behavioral invariance rather than local identifier presence.

## Upgrade and compatibility

Merge the complete hash-locked package/adoption PR, update the local and CI environments to the selected version, and start a fresh agent session.

Consumer CI, naming conventions and branch-protection settings remain consumer-owned; adoption does not configure them automatically.

No breaking changes are included. Read each change's Apply, Compatibility and Notes entry before adopting.

See [versioning and compatibility](https://github.com/phuongnse/engineering-process/blob/v2.7.0/VERSIONING.md) and [adoption guidance](https://github.com/phuongnse/engineering-process/blob/v2.7.0/SELF_HOSTING.md).

[Full change comparison](https://github.com/phuongnse/engineering-process/compare/v2.6.0...v2.7.0)
