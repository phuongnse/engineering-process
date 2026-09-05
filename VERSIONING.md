# Versioning

The package follows SemVer.

- patch: fixes existing behavior without adding a public capability;
- minor: adds backward-compatible CLI, schema, skill, or managed-asset behavior;
- major: removes or incompatibly changes any of those surfaces.

The command tree, exit behavior, lifecycle transitions, JSON Schemas, process lock,
managed files, skill names, and adoption protocol are public API. A release fragment
classifies each change and verification/prepare_release.py derives the next version.

Serialized documents have their own integer schemaVersion. Additive optional fields
may retain a schema version. Removing, renaming, or changing required meaning needs a
new schemaVersion and package-major migration.

Version 1.0 is the intentional clean break from the pre-1.0 governance stack. Its
adoption reader accepts old process locks and project manifests, then writes
process-lock schema 2 and project schema 5. It does not require every intermediate
package version or migration file. Legacy command setup actions are preserved;
process-owned managed-tool installers are dropped because runtimes now belong to the
consumer's host or pinned CI setup.

The old setup shape, doctor --profile, and four read-only publication
validators remain for 1.x only so pre-1.0 consumer CI can validate its first adoption
PR. Removing them requires the next package major.

## Skill namespace migration

The next major release renames the eight delivery/process skills together:

| Previous identifier | New identifier |
| --- | --- |
| run-change | deliver-change |
| start-change | change-start |
| plan-change | change-plan |
| implement-change | change-implement |
| verify-change | change-verify |
| review-change | change-review |
| finish-change | change-complete |
| improve-process | process-improve |

`production-engineering` keeps its name. The new catalog has one delivery entrypoint,
`deliver-change`, six phase skills, and two specializations. Old identifiers are not
aliases in the new catalog. CLI commands, persisted lifecycle states and schemas,
independent-review requirements, and completion receipts are unchanged;
`change-complete` still calls `processctl change finish`. The existing read-only
publication and `doctor --profile` adapters remain available.

Adopt the released package through the normal hash-locked dependency pull request.
Adoption replaces the old owned skill files, writes the new catalog and lock inventory,
and updates the managed AGENTS block. It preserves consumer-owned files, including
files inside the old directories, and rejects conflicting files at new managed paths.
Consumers must update their own explicit skill invocations and custom references using
the mapping above. Restart an existing agent session after adoption so its catalog
matches the installed release. To return to the previous catalog, restore the prior
package pin/hash lock and run that release's adoption transaction.

The producer checkout retains its currently adopted `.agents/skills`, process lock,
and managed AGENTS block until the public adoption pull request. Their old identifiers,
the migration fixtures, and historical evidence remain intentional references to the
previous release; next-distribution sources and documentation use the new catalog.
