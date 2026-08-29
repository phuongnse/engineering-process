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
AGENTS block, and project-schema migration into the branch. Repeating the transaction
must produce no diff.

That branch is an ordinary dependency pull request. It runs normal CI and requires a
reviewer independent of implementation before merge. Merge activates N+1 for later
work. There is no authority-transition protocol, special bootstrap receipt, skipped
release, or target-authored lifecycle proof.

The pre-1.0 managed runner can invoke the 1.0 adoption command directly, so this
repository can move from public 0.9.0 in one PR. The old managed skill tree remains in
the source branch until that public adoption PR; this is intentional distribution
state, not a second editable source tree.
