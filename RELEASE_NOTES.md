# Engineering Process v2.4.0

Changes since v2.3.1.

## Features

- Lifecycle guidance helps consumers find authoritative current project knowledge\, maintain only information affected by accepted work or necessary gaps\, and review explanations and useful connections in context\. Existing consumer organization remains valid\; no documentation taxonomy or migration is required\. ([#192](https://github.com/phuongnse/engineering-process/issues/192))
- Planning\, implementation and review connect tests and other evidence to accepted behavior and concrete risks\, including regression sensitivity and fitting evidence boundaries\. Consumer\-required profiles and testing policies remain authoritative\; no universal test\-first ritual or new test per change is imposed\. ([#193](https://github.com/phuongnse/engineering-process/issues/193))

## Fixes

- Publication\-required changes must commit the complete candidate and pass the existing branch\, subject and nonempty pinned\-range checks before final verification and review\. This avoids a late commit forcing a repeated verification\/review cycle\; exact\-snapshot and finish checks remain enforced\. ([#191](https://github.com/phuongnse/engineering-process/issues/191))

## Upgrade and compatibility

Merge the complete hash-locked package/adoption PR, update the local and CI environments to the selected version, and start a fresh agent session.

Consumer CI, naming conventions and branch-protection settings remain consumer-owned; adoption does not configure them automatically.

See [versioning and compatibility](https://github.com/phuongnse/engineering-process/blob/v2.4.0/VERSIONING.md) and [adoption guidance](https://github.com/phuongnse/engineering-process/blob/v2.4.0/SELF_HOSTING.md).

[Full change comparison](https://github.com/phuongnse/engineering-process/compare/v2.3.1...v2.4.0)
