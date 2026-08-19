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

The package follows SemVer and PEP 440. Its public API is the documented CLI and exit
codes, project and lifecycle schemas, lifecycle transitions, verification and review
evidence, managed distribution assets, and portable execution semantics.

- Patch releases contain only backward-compatible defect fixes.
- Minor releases add backward-compatible public capability or mark a public surface
  deprecated. During the `0.x` pilot, an intentionally incompatible change also uses
  the next minor release, but it requires owner sign-off, migration guidance, and an
  explicit compatibility statement.
- Version `1.0.0` declares the first stable public API. After that point, incompatible
  public changes require a major release.

An integer `schemaVersion` is the compatibility major of one serialized contract,
not the package version. Optional additive properties and validator corrections keep
the current schema number. A schema number changes only when previously valid data
cannot retain the same meaning. Published schema readers remain supported throughout
the current package major; deprecation precedes removal and names a migration and
target major release.

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

## One-time GitHub controls

Complete these controls before publishing the first release:

1. In repository **Settings → General → Releases**, enable release immutability. It
   applies only to releases published after the setting is enabled.
2. Create the `pypi` environment and limit its deployment branches and tags to the
   selected tag pattern `v*`. Do not store a PyPI password or token in the environment.
3. Add a tag ruleset for `refs/tags/v*` that blocks deletion and non-fast-forward
   updates. The release workflow independently requires the tagged commit to be an
   ancestor of `main` and the tag to equal the package version.
4. When the repository has a second trusted maintainer, require that maintainer to
   review `pypi` deployments and enable **Prevent self-review**.

This repository currently has one maintainer, so required deployment review plus
prevent-self-review would deadlock publication. The accepted pilot-release residual
risk is that the sole maintainer can approve their own GitHub release. Exact action
pins, a no-OIDC build job, the complete CI matrix, independent source review, tag
rules, release immutability, and the PyPI publisher identity remain mandatory.

## Release

1. Complete the repository's own process lifecycle on an immutable checkpoint and
   require independent review plus the complete CI matrix to pass. Export the
   completed receipt bound to the pinned public N-1 authority and artifact digests.
2. Update the ordered `release.json.changes`; let their types determine the exact
   SemVer classification. Set the same version in `release.json`, `pyproject.toml`,
   and `engineering_process.VERSION`, and declare canonical artifact/receipt names.
3. Create a draft GitHub release whose tag and title are both exactly
   `v<package-version>` and whose target is the verified `main` commit. Attach the
   receipt using the exact `release.json.identity.receiptAsset` name, then publish the
   immutable release.
4. Observe the `Publish` workflow. It validates the release contract, exact increment,
   latest prior tag, title/tag/checkpoint/package/runtime/artifact identity, receipt
   authority, and `main` ancestry; builds in an isolated tracked snapshot; installs
   the exact inspected wheel; reruns the portable validation suite; and publishes
   both inspected artifacts with PyPI attestations.
5. Confirm the version and artifact hashes on PyPI before updating consumer locks.

Never upload a locally built distribution or enable `skip-existing`. A failed release
is diagnosed and republished as a new version; PyPI artifacts are not replaced.

Release `0.1.1` is explicitly recorded as `bootstrap-history`: its actual immutable
title is retained and it makes no current-lifecycle claim. Because that public N-1
predates `processctl evidence validate`, the first governed receipt uses the one-time
0.1.1 bootstrap path: current code verifies the aggregate/cross-links while the
pinned public authority remains the recorded lifecycle executor. From the next
release onward, the exact pinned N-1 binary must validate the receipt directly; the
workflow fails closed for any other authority without that command.
