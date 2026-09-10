# Engineering Process v2.3.0

Changes since v2.2.0.

## Features

- Generate and verify open and closed issue records from a versioned default or consumer\-owned override while keeping tracker actions and product workflow consumer\-owned\. ([#182](https://github.com/phuongnse/engineering-process/issues/182))
- Opt into publication checks at lifecycle start and completion\, with pinned source metadata in version 2 receipts and exact PR head\/body checks before readiness\; existing consumers and version 1 receipts remain supported\. ([#180](https://github.com/phuongnse/engineering-process/issues/180))

## Fixes

- Require delegated agents and reviewers to preserve the active user\-selected model and reasoning effort\, with native runtime confirmation on spawn and resume and no autonomous model changes\. ([#182](https://github.com/phuongnse/engineering-process/issues/182))
- Resolve bare child commands through the invoking Python environment in fresh sessions\, preserving installed dependencies\, declared argument arrays\, explicit executables and environment filtering\. ([#181](https://github.com/phuongnse/engineering-process/issues/181))

## Upgrade and compatibility

Merge the complete hash-locked package/adoption PR, update the local and CI environments to the selected version, and start a fresh agent session.

Consumer CI, naming conventions and branch-protection settings remain consumer-owned; adoption does not configure them automatically.

See [versioning and compatibility](https://github.com/phuongnse/engineering-process/blob/v2.3.0/VERSIONING.md) and [adoption guidance](https://github.com/phuongnse/engineering-process/blob/v2.3.0/SELF_HOSTING.md).

[Full change comparison](https://github.com/phuongnse/engineering-process/compare/v2.2.0...v2.3.0)
