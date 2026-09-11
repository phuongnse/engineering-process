# Engineering Process v2.3.1

Changes since v2.3.0.

## Fixes

- Refresh package metadata when installing the hash\-locked process so newly published releases remain visible through warm pip caches\; remove the publication\-time cache expiry wait\. Consumer CI using pip 26\.2 or newer should pass \-\-refresh\-package engineering\-process\, or use \-\-no\-cache\-dir with older pip\. ([#134](https://github.com/phuongnse/engineering-process/issues/134))
- Reject recorded agent reviewer\-context reuse across changes in the same repository and its worktrees\. Preserve same\-change correction reviews\, and provide an audited replacement only for a reused initial assignment with no submitted review\. Native fresh\-session and unchanged model\/effort evidence remain required from the host\. ([#182](https://github.com/phuongnse/engineering-process/issues/182))

## Upgrade and compatibility

Merge the complete hash-locked package/adoption PR, update the local and CI environments to the selected version, and start a fresh agent session.

Consumer CI, naming conventions and branch-protection settings remain consumer-owned; adoption does not configure them automatically.

See [versioning and compatibility](https://github.com/phuongnse/engineering-process/blob/v2.3.1/VERSIONING.md) and [adoption guidance](https://github.com/phuongnse/engineering-process/blob/v2.3.1/SELF_HOSTING.md).

[Full change comparison](https://github.com/phuongnse/engineering-process/compare/v2.3.0...v2.3.1)
