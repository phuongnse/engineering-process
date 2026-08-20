# Releasing engineering-process

Public releases are built from an immutable GitHub release tag and uploaded to PyPI
through OpenID Connect. Maintainers do not create or store a PyPI API token.

## One-time PyPI publisher

Before the first release, add a pending trusted publisher for the new PyPI project:

- PyPI project name: `engineering-process`
- GitHub owner: `phuongnse`
- GitHub repository: `engineering-process`
- Workflow filename: `publish.yml`
- Environment name: `pypi`

The GitHub `pypi` environment and the workflow environment must keep the same name.

## Version classification

[`VERSIONING.md`](./VERSIONING.md) is the normative package, serialized-contract,
release, and adoption version policy. Derive the exact candidate with `processctl
publication plan-version`; never choose an increment from release chronology or
development progress.

The root `release.json` is the machine-readable release identity and classification
owner. Its ordered change records derive patch/minor/major classification and
compatibility instead of relying on a manually chosen number alone. It also owns the
package/distribution names, exact tag, GitHub release title, runtime-version source,
wheel/sdist names, lifecycle receipt asset, previous public version, schema impact,
and migration guidance. Governed tag and release title are both exactly `v<SemVer>`.
`processctl contract validate --kind release release.json` validates internal
consistency; `processctl publication validate-release` cross-checks the contract
against `pyproject.toml`, the static runtime constant, receipt, immutable tag and
checkpoint, latest reachable prior release, and `main` ancestry.

The version in `pyproject.toml`, `engineering_process.VERSION`, and `release.json`
changes only in the reviewed release checkpoint. A development commit, schema
clarification, lock regeneration, or failed publication does not advance it by
itself. Consumer process locks change only after the public artifact and its hashes
have been verified.

After publication, Renovate updates `requirements/process.in`, regenerates the
complete `requirements/process.txt` hash graph through pip-compile, and runs the
managed adoption runner before creating its draft. That single PR must contain the
new process lock, managed assets, and any consumer-owned target-version project
migration, pass CI and fresh-context independent review, and then be merged
explicitly. No synchronization is permitted after merge.

## One-time GitHub controls

Complete these controls before publishing the first release:

1. In repository **Settings → General → Releases**, enable release immutability. It
   applies only to releases published after the setting is enabled.
2. Create the `pypi` environment and limit its deployment branches and tags to the
   selected tag pattern `v*`. Do not store a PyPI password or token in the environment.
3. Add a tag ruleset for `refs/tags/v*` that blocks deletion and non-fast-forward
   updates. The release workflow independently requires the tagged commit to be an
   ancestor of `main` and the tag to equal the package version.
4. Add the stable `Change metadata policy` and `Merge eligibility` jobs, observe both
   succeed on one exact pull-request head, then activate the default-branch baseline in
   [`REPOSITORY_GOVERNANCE.md`](./REPOSITORY_GOVERNANCE.md): pull-request-only
   integration, blocked deletion and non-fast-forward updates, no bypass actors, and
   both stable contexts required. During the bootstrap window this exact reviewed
   setting is applied manually by the repository owner; after an immutable release
   contains the adapter, every change uses its compare-before-write plan.
5. When the repository has a second trusted maintainer, require that maintainer to
   review `pypi` deployments and enable **Prevent self-review**.

This repository currently has one maintainer, so required deployment review plus
prevent-self-review would deadlock publication. The accepted pilot-release residual
risk is that the sole maintainer can approve their own GitHub release. Exact action
pins, a no-OIDC build job, the complete CI matrix, independent source review, tag
rules, release immutability, and the PyPI publisher identity remain mandatory.

## Release

1. Complete the repository's own process lifecycle on an immutable checkpoint and
   require independent review plus the complete verification matrix,
   `Change metadata policy`, `Merge eligibility`,
   and live repository policy check to pass. Export the
   completed receipt bound to the pinned public N-1 authority and artifact digests.
2. Update the ordered `release.json.changes`; let their types determine the exact
   SemVer classification. Set the same version in `release.json`, `pyproject.toml`,
   and `engineering_process.VERSION`, and declare canonical artifact/receipt names.
3. Create a draft GitHub release whose existing tag and title are both exactly
   `v<package-version>` and whose target is the verified `main` commit. Attach the
   receipt using the exact `release.json.identity.receiptAsset` name. Run the
   `Prepare release artifacts` workflow for that tag; it validates the draft and N-1 receipt,
   builds from the verified HEAD object graph, attaches the inspected wheel, sdist,
   and digest attestation to the still-editable draft, and fails if any asset exists
   unexpectedly. Only after that workflow passes, publish the immutable release.
4. Observe the `Publish` workflow. It never mutates the published release. Its no-OIDC
   build job downloads the immutable assets, verifies GitHub's release attestation,
   rejects an incomplete or extended immutable asset set, and verifies each local
   asset against the attestation before checking release identity, N-1 receipt,
   artifact digests, installed wheel, and the portable
   validation suite. The separately gated PyPI job downloads and validates those same
   immutable assets again immediately before publishing both distributions with PyPI
   attestations.
5. Confirm the version and artifact hashes on PyPI before updating consumer locks.

Never upload a locally built distribution or enable `skip-existing`. A failed release
is diagnosed and republished as a new version; PyPI artifacts are not replaced.

Release `0.1.1` is explicitly recorded as `bootstrap-history`: its actual immutable
title is retained and it makes no current-lifecycle claim. It predates
`processctl evidence validate`, so it cannot authorize a governed publication that
depends on a lifecycle receipt. There is no fallback to code from the release under
review. Before the first governed release, a separately scoped bootstrap-authority
release must make the receipt validator public and the producer lock must pin that
artifact and its hashes. Until then, publication intentionally fails closed.
