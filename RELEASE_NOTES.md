# Engineering Process v3.0.0

Changes since v2.7.0.

## Breaking changes

- **Support one current process-owned contract definition at version 1** ([#199](https://github.com/phuongnse/engineering-process/issues/199))
  - **Problem:** The process carried multiple schema generations, version branches, legacy readers and writers, migration cleanup, and pre-1.0 publication adapters after active consumers had adopted current releases.
  - **What changed:** Make the latest process-owned definitions the single version-1 contracts, reject other versions, remove obsolete compatibility paths, and preserve current correctness, lifecycle, evidence, adoption, publication, and distribution guarantees.
  - **Apply:** Adopt the released package and exact hash, update current consumer configuration and integration references, regenerate managed artifacts, recreate active process artifacts, and rerun required profiles.
  - **Compatibility:** Breaking process-contract change. The process does not read or convert another release's process-owned artifacts; consumer product compatibility remains consumer-owned.

## Fixes

- **Bound and deduplicate process-improvement intake** ([#228](https://github.com/phuongnse/engineering-process/pull/228))
  - **What changed:** Bind tracker lookup to the complete current consumer/process/invariant/incident identity, reject unsupported destinations and malformed results, distinguish failure from no match, reuse recorded results idempotently, enforce per-finish and per-key limits after reuse, project only typed public evidence, and supervise tracker process cleanup through cancellation and setup failures.
  - **Compatibility:** The current-contract boundary remains a breaking v3.0.0 consumer adoption. The intake correction is non-breaking within the current runtime behavior; consumers must use the current v1 title-key and tracker policy semantics after adoption.

## Upgrade and compatibility

Merge the complete hash-locked package/adoption PR, update the local and CI environments to the selected version, and start a fresh agent session.

Consumer CI, naming conventions and branch-protection settings remain consumer-owned; adoption does not configure them automatically.

Breaking changes are listed above; review each item's Compatibility and Apply guidance before adopting.

See [versioning and compatibility](https://github.com/phuongnse/engineering-process/blob/v3.0.0/VERSIONING.md) and [adoption guidance](https://github.com/phuongnse/engineering-process/blob/v3.0.0/SELF_HOSTING.md).

[Full change comparison](https://github.com/phuongnse/engineering-process/compare/v2.7.0...v3.0.0)
