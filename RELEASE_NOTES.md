# Engineering Process v3.0.0

Changes since v2.7.0.

## Breaking changes

- **Support one current process-owned contract definition at version 1** ([#199](https://github.com/phuongnse/engineering-process/issues/199))
  - **Problem:** The process carried multiple schema generations, version branches, legacy readers and writers, migration cleanup, and pre-1.0 publication adapters after active consumers had adopted current releases.
  - **What changed:** Make the latest process-owned definitions the single version-1 contracts, reject other versions, remove obsolete compatibility paths, and preserve current correctness, lifecycle, evidence, adoption, publication, and distribution guarantees.
  - **Where:** `engineering_process`, `schemas`, `process_assets/skills`, `tests`, `verification`
  - **Apply:** Adopt the released package and exact hash, update current consumer configuration and integration references, regenerate managed artifacts, recreate active process artifacts, and rerun required profiles.
  - **Compatibility:** Breaking process-contract change. The process does not read or convert another release's process-owned artifacts; consumer product compatibility remains consumer-owned.
  - **Notes:** Package/release identity, pin/hash, snapshot binding, evidence freshness, independent review, rollback, and adoption integrity remain unchanged. Historical releases and evidence are not rewritten.

## Fixes

- **Bound and deduplicate process-improvement intake** ([#228](https://github.com/phuongnse/engineering-process/pull/228))
  - **Problem:** Finish-time process-improvement intake could treat tracker search failure as an empty result, reuse a different consumer identity, bypass an existing issue after the creation budget was exhausted, publish untrusted incident details, or leave tracker work running after cancellation.
  - **What changed:** Bind tracker lookup to the complete current consumer/process/invariant/incident identity, reject unsupported destinations and malformed results, distinguish failure from no match, reuse recorded results idempotently, enforce per-finish and per-key limits after reuse, project only typed public evidence, and supervise tracker process cleanup through cancellation and setup failures.
  - **Where:** `engineering_process/incidents.py`, `engineering_process/lifecycle.py`, `tests/test_incidents.py`, `tests/test_lifecycle.py`, `tests/test_contracts.py`, `tests/test_automation.py`, `tests/test_skills.py`, `.github/ISSUE_TEMPLATE/consumer-process-improvement.yml`, `PROCESS_IMPROVEMENT.md`, `README.md`, `process_assets/skills/change-complete/SKILL.md`, `process_assets/skills/process-improve/SKILL.md`
  - **Apply:** Adopt the released package and exact hash, regenerate the current v1 consumer configuration and managed artifacts, recreate active process artifacts, and rerun required profiles. The producer's pinned v2.7.0 snapshot remains until this release is adopted.
  - **Compatibility:** The current-contract boundary remains a breaking v3.0.0 consumer adoption. The intake correction is non-breaking within the current runtime behavior; consumers must use the current v1 title-key and tracker policy semantics after adoption.
  - **Notes:** Tracker search and creation remain evidence handoff only. Unsupported namespaces, failed search, invalid provider URLs, cancellation, and writer failures fail or suppress explicitly without automatic process mutation, merge, release, adoption, or issue closure. Manual issue guidance uses the same injective consumer-key encoding as runtime.

## Upgrade and compatibility

Merge the complete hash-locked package/adoption PR, update the local and CI environments to the selected version, and start a fresh agent session.

Consumer CI, naming conventions and branch-protection settings remain consumer-owned; adoption does not configure them automatically.

Breaking changes are listed above; read each change's Compatibility and Notes entry before adopting.

See [versioning and compatibility](https://github.com/phuongnse/engineering-process/blob/v3.0.0/VERSIONING.md) and [adoption guidance](https://github.com/phuongnse/engineering-process/blob/v3.0.0/SELF_HOSTING.md).

[Full change comparison](https://github.com/phuongnse/engineering-process/compare/v2.7.0...v3.0.0)
