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

1. Merge a reviewed change to `main` and require the complete CI matrix to pass.
2. Set the package version in `pyproject.toml`; released versions are immutable.
3. Create a GitHub release whose tag is exactly `v<package-version>` and whose target
   is the verified `main` commit.
4. Observe the `Publish` workflow. It checks the tag/version and `main` ancestry,
   rebuilds the wheel and source distribution, installs the wheel, reruns the portable
   validation suite, and publishes both artifacts with PyPI attestations.
5. Confirm the version and artifact hashes on PyPI before updating consumer locks.

Never upload a locally built distribution or enable `skip-existing`. A failed release
is diagnosed and republished as a new version; PyPI artifacts are not replaced.
