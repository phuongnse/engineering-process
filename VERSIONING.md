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

The process authority derives the candidate from the ordered public changes and
rejects a mismatched classification, compatibility statement, increment, or schema
impact. Publication additionally requires the declared predecessor to be the latest
reachable final release under the selected version contract.

Development checkpoints do not change package versions. Only a separately reviewed
release checkpoint updates every declared identity surface together. A published
immutable version is never reused; a failed public release is corrected under a newly
derived version. Producer-specific surface mappings belong to the producer release
guide, not this compatibility policy.

Earlier releases whose evidence predates the governed boundary retain explicit
historical provenance and make no retroactive governance claim. Historical provenance
cannot classify a new release as governed.

## Serialized-contract versions

Each artifact owns an independent integer `schemaVersion`. Package SemVer and schema
versions never advance merely because the other one changed.

- An additive optional field or validator correction that preserves every released
  document's meaning keeps the schema version.
- A required field, meaning change, removal, or tighter rule that invalidates a
  previously valid document requires a new schema version.
- A new schema version retains the released reader, documents migration, and adds
  compatibility regressions before it becomes the default example.

A newly introduced serialized artifact starts at `schemaVersion: 1`. An additive
optional field preserves the current schema version only when every previously valid
document retains the same meaning and every historical reader remains supported.

Package `schemaImpact` is `unchanged`, `additive`, or `breaking` for the combined
release. Additive schema capability requires at least a capability release; a
breaking schema requires an incompatible release.

## Release and adoption boundary

Release, self-adoption, and consumer adoption are separate changes regardless of the
selected package registry, source host, automation service, or execution platform:

1. Release N governs and verifies the source of N+1.
2. N+1 is published as an immutable release and its public hashes are verified.
3. The configured adoption adapter updates the authority input, materializes complete
   dependency integrity, and invokes the managed adoption transaction before creating
   one review candidate containing the authority lock and every selected managed
   asset.
4. Project verification validates dependency integrity and the fully materialized
   adoption. Required environment evidence remains bound to the source and verification
   identities. The adoption owner obtains independent review and explicitly integrates
   that same checkpoint. Integration completes adoption; there is no post-integration
   synchronization.
5. N+1 governs only changes that begin after the adoption checkpoint.

Automation-generated review objects are adoption candidates, not trusted adoption
evidence. Automatic integration is forbidden for process-authority updates. A
candidate that changes only an authority input, omits dependency integrity or managed
assets, or requires a later synchronization step fails closed.

The adoption adapter binds installation and application to one stable private input,
contains child execution through the portable task boundary, and atomically
synchronizes the authority lock and managed assets. Concurrent input changes or
partial materialization fail and roll back. Code under development never acts as its
own adoption authority; concrete acquisition, locking, and containment mechanisms
belong to adapter owners.

Consumer-owned project policy is never inferred. When a release needs project
configuration activation, the consumer supplies one bounded declarative migration
that binds the exact source and target authorities plus the complete target project
contract. Only the installed target authority validates and applies it. Project
configuration and all managed targets share one rollback transaction; stale identity,
wrong target version, invalid configuration, concurrent mutation, or partial write
fails closed. The migration remains durable review evidence and repeated adoption is
idempotent. Activation is reviewed in the same candidate and never deferred until
after integration. Optional capabilities without a declared migration remain
disabled; missing required configuration blocks adoption.

New public contracts, stable gate semantics, adapter interfaces, and authority
commands are capabilities and require a compatible release classification. A change
to an existing meaning is breaking even when its serialized shape is unchanged.
Development introduces these surfaces without changing package identity; the later
release contract records each public change exactly once.

Consumer adoption and activation of external integration policy remain separate
changes with separate authorization. Updating a process authority never grants an
external permission or mutates a hosted system implicitly. If the selected adoption
adapter cannot materialize the complete candidate, adoption remains blocked rather
than falling back to a partial proposal.

## Responsibility

- Change owners classify public behavior; they do not choose a version number.
- The release owner freezes scope and records the exact ordered change set.
- The process authority derives and validates classification, compatibility, and
  identity.
- An adoption adapter may generate a complete review candidate but has no integration
  authority.
- Independent review verifies the classification and migration evidence.
- The repository owner alone authorizes release and adoption integration.
