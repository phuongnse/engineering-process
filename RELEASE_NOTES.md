# Engineering Process v2.2.0

Changes since v2.1.0.

## Features

- Generate and verify PR descriptions and release notes from versioned standards with consumer\-owned overrides preserved by adoption\. Matching template and validator definitions reject unresolved default PR placeholders in ready state\; custom formats retain consumer\-owned validation profiles\. ([#174](https://github.com/phuongnse/engineering-process/issues/174))
- Generate and verify automation names from a default owner\-role convention or a consumer\-owned override\. Bootstrap integrations can consume structured CLI output and check actual provider names before side effects\; provider limits and authenticated identity remain consumer\-owned\. ([#176](https://github.com/phuongnse/engineering-process/issues/176))

## Upgrade and compatibility

Merge the complete hash-locked package/adoption PR, update the local and CI environments to the selected version, and start a fresh agent session.

Consumer CI, naming conventions and branch-protection settings remain consumer-owned; adoption does not configure them automatically.

See [versioning and compatibility](https://github.com/phuongnse/engineering-process/blob/v2.2.0/VERSIONING.md) and [adoption guidance](https://github.com/phuongnse/engineering-process/blob/v2.2.0/SELF_HOSTING.md).

[Full change comparison](https://github.com/phuongnse/engineering-process/compare/v2.1.0...v2.2.0)
