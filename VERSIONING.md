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
