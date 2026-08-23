# Version governance

This document is the normative owner for engineering-process package versions,
serialized-contract versions, and consumer adoption. Version numbers are derived
from reviewed public impact; they are never used as a progress counter.

## Package version

`release.json` is the single release-classification and identity source. Every
public change in a release is recorded exactly once as `fix`, `capability`, or
`breaking`. The highest-impact type determines the only permitted next SemVer:

| Highest public change | Compatibility | Required increment |
| --- | --- | --- |
| `fix` | backward-compatible | patch |
| `capability` | backward-compatible | minor |
| `breaking` before 1.0 | incompatible | minor |
| `breaking` from 1.0 onward | incompatible | major |

An incompatible release requires migration guidance and owner sign-off. A release
with no distributable public change does not advance the package version. Multiple
changes in one release do not create multiple increments.

Every feature or fix with distributable public impact owns one bounded
`release-changes/<id>.json` fragment. The fragment records the public change type,
surfaces, schema impact, rationale, and any required breaking migration. Change owners
classify behavior in their reviewed feature PR; they never edit a package version.

Preview the derivation without editing a version surface:

~~~text
processctl publication plan-version --previous-version 0.1.1 \
  --change-type capability --change-type fix --json
~~~

After fragments reach `main`, `processctl publication prepare-release` aggregates the
bounded ordered set into one generated Release PR. It independently derives the same
result and updates `release.json`, `pyproject.toml`, `engineering_process.VERSION`,
artifact names, evidence names, and generated release lifecycle inputs together.
`processctl contract validate --kind release release.json` rejects a mismatched
classification, compatibility statement, increment, or schema impact. Publication
additionally requires the declared previous version to be the latest reachable final
SemVer tag.

Development commits do not change package versions. Only the separately reviewed
generated Release PR updates all identity surfaces together. Its merge is the sole
publication authorization; every post-merge action is deterministic automation. A
published immutable or PyPI version is never reused, and a failed published release is
corrected under a newly derived version.

The transition from recorded `bootstrap-history` to the first public lifecycle
authority uses release schema 3 mode `bootstrap-authority` exactly once. Its separately
typed authorization bundle is not a lifecycle receipt. Self-adoption must pin that
public version before another release can be prepared; every later release is
`governed` by a receipt from its public N-1 authority.

## Serialized-contract versions

Each artifact owns an independent integer `schemaVersion`. Package SemVer and schema
versions never advance merely because the other one changed.

- An additive optional field or validator correction that preserves every released
  document's meaning keeps the schema version.
- A required field, meaning change, removal, or tighter rule that invalidates a
  previously valid document requires a new schema version.
- A new schema version retains the released reader, documents migration, and adds
  compatibility regressions before it becomes the default example.

A newly introduced artifact starts at `schemaVersion: 1`. The supplemental
verification manifest and project-adoption migration each follow that rule. Adding
optional `timeoutSeconds` evidence to verification schema 2 preserves existing
schema-2 documents and therefore does not advance that artifact's schema version.

Every generator must be qualified against the exact released reader that consumes its
output. For a lifecycle artifact, validation at creation is insufficient: CI runs the
generated change, plan, review, completion, and exported evidence through public N-1
up to the final consuming transition. Review schema and quality mappings derive from
the registered change contract instead of being maintained as an independent version
constant.

Package `schemaImpact` is `unchanged`, `additive`, or `breaking` for the combined
release. Additive schema capability requires at least a capability release; a
breaking schema requires an incompatible release.

## Release and adoption boundary

Release, self-adoption, and consumer adoption are separate changes:

1. Release N governs and verifies the source of N+1.
2. N+1 is published as an immutable release and its public hashes are verified.
3. Renovate or another consumer-owned automation prepares the complete candidate
   branch without creating a PR, updates the direct pin, regenerates the hash-locked
   dependency graph, and runs the managed adoption runner.
4. Required profiles validate the immutable candidate. The consumer-selected host
   supplies an independent semantic agent or human review; findings repeat candidate
   generation and full verification until lifecycle completion. Each
   platform/runtime job publishes a bounded supplemental evidence bundle bound to the
   source checkpoint, workflow checkpoint, run identity, and profile-report hashes.
   Only after completion may automation create the PR containing the process lock and
   every selected managed asset. The consumer owner explicitly merges that exact
   checkpoint; there is no post-merge synchronization.
5. N+1 governs only changes that begin after the merged adoption checkpoint.

The repository-root GitHub Action and Python package are two surfaces of the same
governed release checkpoint. A consumer invocation pins the action by the release
commit's full object id and keeps the `v<SemVer>` annotation; Renovate groups that
GitHub Action identity with the direct Python authority update. The action does not
select the package version. It installs only the consumer's complete hash lock, so
the process lock, Python artifacts, managed assets, action source, and reviewed tag
remain independently checkable parts of one release identity.

Consumer repositories do not own copies of reusable installation or publication
algorithms. They retain declarative process/project locks, exact project commands,
and managed bootstrap snapshots required before a target version is installed. A
clean-cutover PR may delete an obsolete local helper only after it pins an immutable
public action checkpoint; no compatibility shim or dual execution path is retained.

Renovate branches are generated adoption candidates, not trusted adoption evidence.
PR creation is blocked until the candidate has current completion evidence.
Automerge is forbidden for process-authority updates. A PR that changes only a
requirement pin, omits generated hashes or managed assets, or requires a post-merge
step must fail closed.

`requirements/process.in` owns the direct public pin and `requirements/process.txt`
is its pip-compile hash lock. The lock generator is pinned, and the committed lock must
cover compatible binary artifacts for every supported Python/OS runner rather than
only the platform that compiled it. Renovate's exact allowlisted post-upgrade command runs
`.process/adopt-process.py`; that managed runner creates a bounded temporary
environment outside the checkout, rejects symlink, junction, or reparse input in
every supplied lock-path component, and makes one stable private snapshot. Component
identities reject concurrent parent retargeting. Both the binary-only
installation and the installed distribution's `processctl adoption apply` are bound
to that snapshot digest; a live-lock change fails and rolls back materialization.
POSIX process groups and the side-by-side managed Windows kill-on-close Job Object
helper bound descendant lifetimes. The command preserves selected optional skills,
adds newly mandatory core skills, regenerates `.process/process.lock`, and
synchronizes all managed assets in the draft. Never run the checkout under
development as the adoption authority.

Consumer-owned project policy is never inferred. When a release needs project
configuration activation, the consumer adds one bounded declarative migration at
`.process/adoption-migrations/<target-version>.json`. It binds the exact previous and
target process versions, source and target project-manifest digests, and the complete
target manifest. Only the installed target distribution validates and applies its
exact file. The active project manifest and all managed targets share one rollback
transaction; a stale source digest, wrong target version, invalid target manifest,
concurrent mutation, or partial write fails closed. The migration remains as durable
review evidence and repeated adoption is idempotent. Renovate allowlists both the
migration and `.process/project.json`, so activation is reviewed in the same draft
and never deferred until after merge. Optional capabilities without a declared
migration remain disabled; configuration required by the target distribution must
validate or adoption is blocked.

Validate a prepared migration before the Renovate runner consumes it:

~~~text
processctl contract validate --kind adoption-migration \
  .process/adoption-migrations/<target-version>.json
~~~

The Renovate administrator must allow only the literal managed runner command. If
post-upgrade commands are unavailable or the pip-compile artifact update fails,
Renovate cannot produce an adoptable PR and the update remains blocked rather than
falling back to a partial proposal.

## Responsibility

- Change owners classify public behavior; they do not choose a version number.
- Release PR automation freezes the exact ordered fragment set and materializes every
  derived identity surface without writing protected `main` directly.
- `processctl` derives and validates classification, compatibility, identity, reviewed
  tree equivalence, and authorization evidence.
- Renovate generates complete draft adoption candidates without merge authority.
- Independent review verifies the exact Release PR classification, migration,
  verification, and evidence checkpoint.
- The repository owner authorizes publication by merging the Release PR and separately
  authorizes each self-adoption merge. Consumer owners retain their adoption merge
  policy.
