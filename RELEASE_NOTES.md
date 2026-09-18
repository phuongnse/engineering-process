# Engineering Process v3.2.1

Changes since v3.2.0.

## Fixes

- **Make release-note source links durable and visible detail labels standard-owned.** ([#246](https://github.com/phuongnse/engineering-process/issues/246))
  - **Result:** Release-note source records now require durable HTTPS references, safe rendering keeps them clickable, and detailLabels in the selected standard controls the visible labels for each selected detail field.
  - **Impact:** The current packaged release-notes standard remains version 1; existing custom standards without detailLabels fall back to field IDs, while new or edited records must use HTTPS sources.
- **Preserve the Windows user-local application context required by bounded Python checks.** ([#245](https://github.com/phuongnse/engineering-process/issues/245))
  - **Result:** The managed child environment now preserves LOCALAPPDATA alongside the existing explicit Windows runtime variables, with regression coverage for the Python bootstrap boundary.
  - **Impact:** Non-breaking for the current v1 lifecycle contract; Windows checks receive one additional documented runtime-resolution input and unrelated ambient variables remain excluded.

## Upgrade and compatibility

Merge the complete hash-locked package/adoption PR, update the local and CI environments to the selected version, and start a fresh agent session.

Consumer CI, naming conventions and branch-protection settings remain consumer-owned; adoption does not configure them automatically.

No breaking changes are included. Review each item's Impact and follow any shown Adopt guidance before adopting.

See [versioning and compatibility](https://github.com/phuongnse/engineering-process/blob/v3.2.1/VERSIONING.md) and [adoption guidance](https://github.com/phuongnse/engineering-process/blob/v3.2.1/SELF_HOSTING.md).

[Full change comparison](https://github.com/phuongnse/engineering-process/compare/v3.2.0...v3.2.1)
