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

A newly introduced artifact starts at `schemaVersion: 1`. The project-adoption
migration follows that rule. Adding optional `timeoutSeconds` evidence to
verification schema 2 preserved existing schema-2 documents and therefore did not
advance that artifact's schema version.

Requiring diagnostic classification changes what a passing command and its evidence
mean. Current generators therefore emit verification schema 3, where every check
requires a redacted diagnostic summary, and supplemental-verification schema 2,
where every embedded check summary binds that new evidence and every report is schema
3. Verification schema 1/schema 2 and supplemental schema 1 remain readable with
their released meaning; they are historical evidence, not alternate current
generation formats. Consumers upgrading to this authority must correct warnings and
errors in their canonical commands and update integrations that generate current
verification or supplemental documents to the new majors. They must not add
suppression or translate new reports back into an older shape.

Every generator must be qualified against the exact released reader that consumes its
output. For a lifecycle artifact, validation at creation is insufficient: CI runs the
generated change, plan, review, completion, and exported evidence through public N-1
up to the final consuming transition. Review schema and quality mappings derive from
the registered change contract instead of being maintained as an independent version
constant.

Package `schemaImpact` is `unchanged`, `additive`, or `breaking` for the combined
release. Additive schema capability requires at least a capability release; a
breaking schema requires an incompatible release.

## Controlled dependency-proposal capability

The strict default remains completion before any branch or PR publication. A consumer
may adopt the additive controlled automation-proposal capability through a separate,
completed configuration change on protected main. Its base-owned policy and immutable
verifier bind one untrusted dependency proposal to exact repository, base/head, paths,
metadata, controls, and verifier identity. Proposal creation is not lifecycle evidence
or authority. The adapter supplies the actual protected-base commit independently from
the provider event so the report cannot select a stale policy object. Automerge,
scripts, plugins, shell execution, privileged/write-capable
proposal checks, and process-authority, workflow, release, deployment, security-policy,
or trust-root changes remain excluded.

Branch protection requires the canonical `lifecycle-completion` check, which is absent
for every new proposal SHA. The provider adapter may create it only after
`publication validate-proposal-completion` validates the same proposal policy and an
external completion receipt for the exact clean head. Schema-1 policy preserves the
historical mandatory human merge. Schema 2 instead requires the protected base's valid
standing automation policy and permits exact-head merge only after the completion
check and every protected-branch gate pass.
Branch protection also requires the proposal to be current with the exact validated
base, so a base advance forces a new head and invalidates the old completion check.
The producer capability must be released immutably before a consumer pins and enables
it; no consumer may depend on the producer working tree.

## Federated process-improvement capability

Improvement signals, dispositions, resolutions, reproductions, and the producer
catalog are independent schema-1 artifacts. Their introduction is additive schema
surface, while automatically blocking corrective lifecycle progress until a validated
failure has an explicit disposition changes lifecycle semantics. Before 1.0 this is a
breaking package change and therefore requires a minor release plus migration guidance.

Older lifecycle, completion, receipt, verification, and supplemental documents keep
their released meaning. Improvement references are additive to historical state and
completion readers; current generation emits them only when a failure or finding has
created a case. Consumers pinned to older authorities do not gain the new behavior
until a separately completed adoption pins an immutable release.

Producer completion does not create an improvement resolution. Resolution additionally
binds the immutable public release, and shared aggregate closure additionally binds a
consumer reproduction after adoption. A read-only pre-release consumer candidate is
supplemental compatibility evidence only and cannot be committed as an unreleased
dependency.

## Recommendation-validity capability

Recommendation, review-assignment, review, and owner-resolution schema-1 artifacts
are additive serialized surfaces. Requiring every new material owner decision to use
their invariant derivation, project-global fresh-context reservation, independent
challenge, chain validation, and valid resolution changes managed workflow semantics.
Before 1.0 this is therefore a breaking package change even though `schemaImpact`
remains additive.

After adoption, new material decisions use `recommendation review start` before the
reviewer acts, validate the exact assignment-bound chain before presenting a
recommendation, and record the selected valid option before dependent work resumes.
Completed historical decisions retain their original evidence and require no
backfill. A recommendation resolution is decision evidence only and never substitutes
for lifecycle completion or delivery authority.

Remote-verification request and evidence-set schema-1 artifacts, optional project
requirements, optional change selections, lifecycle references, and receipt content
are additive serialized surfaces. A project does not gain the gate until a separate
adoption validates the target-version manifest and selects requirement ids. Once
selected, missing exact remote evidence blocks review and completion; a later PR
matrix is not a compatibility substitute. Provider transport remains project-owned,
while request expansion, ingestion, invalidation, and terminal ordering are portable
process semantics.

## Release and adoption boundary

Release, self-adoption, and consumer adoption are separate changes:

1. Release N governs and verifies the source of N+1.
2. N+1 is published as an immutable release and its public hashes are verified.
3. Consumer-owned automation prepares an unpublished local candidate artifact,
   updates the direct pin, regenerates the hash-locked dependency graph, and runs the
   managed adoption runner. Renovate must not publish a process-authority branch or
   PR because its normal PR-first execution cannot satisfy this lifecycle boundary.
4. Required profiles validate the immutable candidate. The consumer-selected host
   supplies an independent semantic agent or human review; findings repeat candidate
   generation and full verification until lifecycle completion. Each
   platform/runtime job publishes a bounded supplemental evidence bundle bound to the
   source checkpoint, workflow checkpoint, run identity, and profile-report hashes.
   Only after completion may automation push the candidate branch and create the PR
   containing the process lock and every selected managed asset. A valid consumer
   standing policy may merge that exact checkpoint automatically after required
   checks; there is no post-merge synchronization.
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

Process-authority adoption candidates are not controlled dependency proposals and
cannot use that exception. PR creation is blocked until the adoption candidate has
current completion evidence.
Provider automerge before completion is forbidden for process-authority updates. A
completed exact adoption candidate may merge automatically only under the consumer's
standing policy. A PR that changes only a requirement pin, omits generated hashes or
managed assets, or requires a post-merge step must fail closed.

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
- Consumer automation may prepare complete unpublished adoption candidates before
  completion; the lifecycle host publishes and, under standing policy, merges them
  only after exact completion and protected checks.
- Independent review verifies the exact Release PR classification, migration,
  verification, and evidence checkpoint.
- The repository's standing policy authorizes the exact completed Release PR and each
  completed self-adoption merge. Consumer owners retain and explicitly install their
  own automation policy; publishers never infer it.
