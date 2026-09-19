# Engineering Process v3.2.2

Changes since v3.2.1.

## Fixes

- **Allow hash-locked consumers to adopt from an older process lock.** ([#248](https://github.com/phuongnse/engineering-process/issues/248))
  - **Result:** Adoption now treats a schema-valid older engineering-process lock as migration input, converges it to the installed distribution, and continues to reject newer locks and current-version distribution mismatches.
  - **Impact:** Non-breaking for consumers upgrading from an older final process release; invalid, future, foreign, or current-version mismatched lock state remains rejected before mutation.

## Upgrade and compatibility

Merge the complete hash-locked package/adoption PR, update the local and CI environments to the selected version, and start a fresh agent session.

Consumer CI, naming conventions and branch-protection settings remain consumer-owned; adoption does not configure them automatically.

No breaking changes are included. Review each item's Impact and follow any shown Adopt guidance before adopting.

See [versioning and compatibility](https://github.com/phuongnse/engineering-process/blob/v3.2.2/VERSIONING.md) and [adoption guidance](https://github.com/phuongnse/engineering-process/blob/v3.2.2/SELF_HOSTING.md).

[Full change comparison](https://github.com/phuongnse/engineering-process/compare/v3.2.1...v3.2.2)
