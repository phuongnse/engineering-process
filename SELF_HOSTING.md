# Self-hosting

This repository is both producer and consumer, using the same files as any other
consumer:

- requirements/process.in and requirements/process.txt pin the latest adopted public
  package;
- .process/process.lock binds that distribution;
- .agents/skills contains its managed skills;
- .github/renovate.json opts into package updates.

Source under process_assets/skills and engineering_process is the next candidate. It
is tested directly but does not overwrite the managed consumer copy during ordinary
development.

When N+1 is published, the release workflow triggers renovate-ops. Renovate updates
the exact package pin and hash lock, then the existing .process/adopt-process.py
installs N+1 in an isolated environment. N+1 synchronizes its skills, adopter, lock,
AGENTS block, and current project configuration into the branch. Repeating the
transaction must produce no diff; a consumer recreates unsupported process artifacts
under the new current contract.
Consumer document-standard selections and definitions survive adoption. The generated
PR-template block follows the effective definition; custom bot-body configuration is
still consumer-owned and must match that standard before relying on its drafts.

That branch is an ordinary dependency pull request. It runs normal CI and requires a
reviewer independent of implementation before merge. Merge activates N+1 for later
work. There is no authority-transition protocol, special bootstrap receipt, skipped
release, or target-authored lifecycle proof.
Renovate's draft is the handoff to the consumer. Its author/coordinator fills actual
contract, verification, review and receipt results before ready/merge; unchecked boxes
and pending fields are not a completed adoption claim.

Adoption changes follow the proportional verification model:
1. Verify adoption integrity (`processctl adoption check`, hash lock, doctor) and integration boundaries.
2. For fast feedback on product changes, declare consumer-owned `impactProfiles` units and use `processctl change explain --impact` followed by `processctl change verify --affected`. Every changed path must resolve; an unmapped path blocks the affected run and requires diff/dependency re-analysis, never an automatic full fallback.
3. When consumer product sources are unchanged and prior profile evidence matches the candidate and runtime environment, use `processctl change verify --remaining` to satisfy required profiles proportionally without rerunning unaffected whole profiles.
4. A consumer may opt selected required profiles into final impact assurance with the
   current version-1 `impactProfiles` and `finalProfiles` only after declaring complete
   path coverage and an explicit global unit whose paths include the universal
   double-star (`**`) pattern for cross-cutting reach. Unresolved final coverage blocks
   `--remaining`; explicit profile refresh remains available.
5. Mixed adoption with consumer product code or policy modifications still requires the full normal verification matrix unless the reviewed final assurance policy explicitly covers those changes.
6. Baseline required profiles are never omitted at change start.

The managed skill tree under `.agents/skills` represents the currently adopted public
release; `process_assets/skills` contains the source for the next release candidate.
Their byte equality is normal post-adoption distribution state, not a second editable source tree.
