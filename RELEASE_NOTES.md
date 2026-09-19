# Engineering Process v3.2.3

Changes since v3.2.2.

## Fixes

- **Preserve standard Windows runtime inputs in bounded consumer checks.** ([#245](https://github.com/phuongnse/engineering-process/issues/245))
  - **Result:** The managed projection now retains OS, ProgramFiles, and ProgramFiles(x86) alongside LOCALAPPDATA, while continuing to exclude unrelated ambient values, Python path injection, and secret-marked inputs.
  - **Impact:** Non-breaking additive patch release. Existing v3.2.2 locks remain valid; consumers must adopt v3.2.3 to receive the corrected Windows projection, while arbitrary ambient environment state remains excluded.

## Upgrade and compatibility

Merge the complete hash-locked package/adoption PR, update the local and CI environments to the selected version, and start a fresh agent session.

Consumer CI, naming conventions and branch-protection settings remain consumer-owned; adoption does not configure them automatically.

No breaking changes are included. Review each item's Impact and follow any shown Adopt guidance before adopting.

See [versioning and compatibility](https://github.com/phuongnse/engineering-process/blob/v3.2.3/VERSIONING.md) and [adoption guidance](https://github.com/phuongnse/engineering-process/blob/v3.2.3/SELF_HOSTING.md).

[Full change comparison](https://github.com/phuongnse/engineering-process/compare/v3.2.2...v3.2.3)
