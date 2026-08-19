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

Derive the candidate before editing any version surface:

~~~text
processctl publication plan-version --previous-version 0.1.1 \
  --change-type capability --change-type fix --json
~~~

`processctl contract validate --kind release release.json` independently derives the
same result from `release.json.changes` and rejects a mismatched classification,
compatibility statement, increment, or schema impact. Publication additionally
requires the declared previous version to be the latest reachable final SemVer tag.

Development commits do not change package versions. Only a separately reviewed
release checkpoint updates all identity surfaces together: `release.json`,
`pyproject.toml`, `engineering_process.VERSION`, tag, release title, wheel, sdist,
receipt, and attestation names. A published immutable or PyPI version is never
reused; a failed published release is corrected under a newly derived version.

## Serialized-contract versions

Each artifact owns an independent integer `schemaVersion`. Package SemVer and schema
versions never advance merely because the other one changed.

- An additive optional field or validator correction that preserves every released
  document's meaning keeps the schema version.
- A required field, meaning change, removal, or tighter rule that invalidates a
  previously valid document requires a new schema version.
- A new schema version retains the released reader, documents migration, and adds
  compatibility regressions before it becomes the default example.

Package `schemaImpact` is `unchanged`, `additive`, or `breaking` for the combined
release. Additive schema capability requires at least a capability release; a
breaking schema requires an incompatible release.

## Release and adoption boundary

Release, self-adoption, and consumer adoption are separate changes:

1. Release N governs and verifies the source of N+1.
2. N+1 is published as an immutable release and its public hashes are verified.
3. Renovate updates the direct input pin, regenerates the complete hash-locked
   dependency graph, and runs the managed adoption runner before it creates one
   draft PR containing the process lock and every selected managed asset.
4. CI validates dependency integrity and the fully materialized adoption, then the
   adoption owner obtains independent review and explicitly merges that same
   checkpoint. Merge completes adoption; there is no post-merge synchronization.
5. N+1 governs only changes that begin after the adoption checkpoint.

Renovate PRs are generated adoption candidates, not trusted adoption evidence.
Automerge is forbidden for process-authority updates. A PR that changes only a
requirement pin, omits generated hashes or managed assets, or requires a post-merge
step must fail closed.

`requirements/process.in` owns the direct public pin and `requirements/process.txt`
is its pip-compile hash lock. Renovate's exact allowlisted post-upgrade command runs
`.process/adopt-process.py`; that managed runner creates a bounded temporary
environment outside the checkout, rejects symlinked lock input, and makes one stable
private snapshot. Both the binary-only installation and the installed distribution's
`processctl adoption apply` are bound to that snapshot digest; a live-lock change
fails and rolls back materialization. POSIX process groups and the side-by-side
managed Windows kill-on-close Job Object helper bounds descendant lifetimes. The
command preserves selected optional skills, adds newly mandatory core skills,
regenerates `.process/process.lock`, and synchronizes all managed assets in the draft.
Never run the checkout under development as the adoption authority.

The Renovate administrator must allow only the literal managed runner command. If
post-upgrade commands are unavailable or the pip-compile artifact update fails,
Renovate cannot produce an adoptable PR and the update remains blocked rather than
falling back to a partial proposal.

## Responsibility

- Change owners classify public behavior; they do not choose a version number.
- The release owner freezes scope and records the exact ordered change set.
- `processctl` derives and validates classification, compatibility, and identity.
- Renovate generates complete draft adoption candidates without merge authority.
- Independent review verifies the classification and migration evidence.
- The repository owner alone authorizes release and adoption merges.
