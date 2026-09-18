# Versioning

This is the maintainer reference for current contract boundaries and consumer
recovery. New consumers should start at [README.md](README.md); adoption steps
are in [SELF_HOSTING.md](SELF_HOSTING.md), and documentation source/output
ownership is in [docs/documentation.md](docs/documentation.md).

The package keeps its public release identity under SemVer. A release may contain a
breaking process change and its release record must describe the consumer action. The
package version, Git tag, process lock pin, distribution digest, and release snapshot
remain distinct identities and are never inferred from a contract `schemaVersion`.

## Current contract boundary

Every artifact, schema, and serialized contract owned by engineering-process has one
current definition with version `1`. This includes project configuration, change and
plan documents, review reports, lifecycle runs and receipts, verification selections,
release manifests and fragments, process locks, process graph data, and artifact
standard definitions. The latest supported semantics are edited directly in that
definition, including breaking changes.

The runtime validates the current definition directly and rejects another version or
data that does not satisfy the current JSON Schema. It has no version dispatch, legacy
reader/writer, compatibility adapter, migration framework, field guessing, or silent
fallback for process-owned formats. A `schemaVersion: 1` value is not evidence that a
document or evidence record from another package release can be reused; digest,
authority, input, candidate, and snapshot bindings still have to match.

Consumer repositories own the adoption boundary. For a release that changes a current
contract, a consumer updates its exact package/version/hash pin, rewrites its current
configuration and integration references, regenerates managed artifacts, recreates
any active process artifacts, and reruns the required profiles. The process does not
convert unsupported consumer data or promise backward compatibility between releases.
Consumer product compatibility remains the product owner's responsibility.

The current skill catalog is:

- `deliver-change` as the delivery entrypoint;
- `change-start`, `change-plan`, `change-implement`, `change-verify`,
  `change-review`, and `change-complete` for the six lifecycle operations;
- `process-improve` and `production-engineering` as lifecycle specializations.

Historical skill names, schemas, runs, receipts, and release records remain in the
commits that published them. They are historical evidence only and are not aliases or
inputs for the current runtime. The repository does not rewrite those records to use
the current contract.

## Release records

The current release-fragment and release-manifest definitions both use
`schemaVersion: 1` and require complete detail fields (`problem`, `changes`,
`affectedPaths`, `apply`, `compatibility`, and `notes`). `verification/prepare_release.py`
accepts only that current fragment shape and emits the current manifest shape. It still
derives the package's next SemVer from the consumer-owned change classification; it
does not change artifact or contract versions.

Release contents are generated from the manifest and checked byte-for-byte. Existing
release identity, rendered notes, package metadata, and historical evidence are not
rewritten as part of interpreting an older release. A future release owner decides
when to publish a new package and what release record it contains.

## Adoption

Adoption writes the current managed skill catalog, current lock, current project
configuration, templates, and managed instructions in one transactional operation.
It preserves consumer-owned files and verifies the selected package version, process
distribution digest, requirements digest, and managed-file inventory. A lock or
configuration from another contract release fails validation; consumers recreate it
under the current definition instead of relying on process-side conversion.

The adopter keeps the bounded command runner, rollback, collision protection, platform
cleanup, hash locking, and snapshot checks. It does not discover or delete old
migration directories or obsolete managed files on behalf of a consumer.

## Publication and evidence

The current publication checks are read-only checks for the consumer's typed branch,
commit, range, and selected PR document. `doctor` validates the current project,
lock, digest, and adoption state; it has no historical profile adapter. Publication
conventions remain consumer-owned and can be disabled or replaced by the consumer's
own required profile.

Lifecycle evidence remains bound to the exact unchanged candidate, accepted contract,
plan, current process authority, runtime, dependency/environment identity, comparison
base, and required profiles. Independent review still rejects reviewer actor/context
reuse, and completion still requires fresh evidence, current publication state, and
all applicable readiness/adoption guarantees.

The producer checkout's `.agents/skills`, process lock, and managed instructions are
consumer adoption state. `process_assets/skills` and the packaged schemas are the
next distribution source. Updating a source definition does not mutate a prior
adoption snapshot or receipt.
