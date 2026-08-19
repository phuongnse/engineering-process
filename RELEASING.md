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

The root `release.json` is the machine-readable release specification. It must name
the exact previous public version, next SemVer increment, compatibility and schema
impact, and migration guidance for every incompatible release. `processctl contract
validate --kind release release.json` validates its shape and classification;
`processctl publication validate-release` additionally binds it to
`pyproject.toml`, the immutable release tag and checkpoint, the latest reachable
prior release, and `main` ancestry.

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
   require independent review plus the complete CI matrix to pass.
2. Update `release.json` with the SemVer classification and any schema compatibility
   or migration impact, then set the same package version in `pyproject.toml` and
   `engineering_process.VERSION`; released versions are immutable.
3. Create a GitHub release whose tag is exactly `v<package-version>` and whose target
   is the verified `main` commit.
4. Observe the `Publish` workflow. It validates the release contract, exact increment,
   latest prior tag, tag/checkpoint identity, and `main` ancestry; rebuilds the wheel
   and source distribution; installs the wheel; reruns the portable validation suite;
   and publishes both artifacts with PyPI attestations.
5. Confirm the version and artifact hashes on PyPI before updating consumer locks.

Never upload a locally built distribution or enable `skip-existing`. A failed release
is diagnosed and republished as a new version; PyPI artifacts are not replaced.
