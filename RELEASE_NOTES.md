# Engineering Process v2.1.0

Changes since v2.0.2.

## Features

- Pin comparison refs in new lifecycle runs\, require explicit blocker closure\, and wire the producer\'s actual publication metadata into required verification while retaining older run readers\. ([#169](https://github.com/phuongnse/engineering-process/issues/169))

## Fixes

- Carry consumer design quality through accepted criteria\, planning\, implementation\, and independent review\, with proactive cohesive abstractions and evidence\-based maintenance judgment\. ([#119](https://github.com/phuongnse/engineering-process/issues/119))
- Clarify real independent reviewer dispatch\, neutral context and reviewer\-owned report handoff using the existing runner\. ([#166](https://github.com/phuongnse/engineering-process/issues/166))
- Publish reviewed release contents with grouped changes\, source issues and upgrade guidance generated from the canonical release manifest\. ([#171](https://github.com/phuongnse/engineering-process/issues/171))

## Upgrade and compatibility

Merge the complete hash-locked package/adoption PR, update the local and CI environments to the selected version, and start a fresh agent session.

Consumer CI, naming conventions and branch-protection settings remain consumer-owned; adoption does not configure them automatically.

See [versioning and compatibility](https://github.com/phuongnse/engineering-process/blob/v2.1.0/VERSIONING.md) and [adoption guidance](https://github.com/phuongnse/engineering-process/blob/v2.1.0/SELF_HOSTING.md).

[Full change comparison](https://github.com/phuongnse/engineering-process/compare/v2.0.2...v2.1.0)
