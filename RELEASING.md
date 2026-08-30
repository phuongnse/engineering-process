# Releasing

A release has one authorization boundary: merge of a normal version-changing Release
PR after CI and independent review.

## Prepare

Every public change adds one release-changes/*.json fragment with type fix,
capability, or breaking. The highest type derives the next version:

- fix: patch;
- capability: minor;
- breaking: major.

Run the Prepare release PR workflow with that exact version. It executes:

    python verification/prepare_release.py VERSION

The script validates every fragment, updates pyproject.toml,
engineering_process.VERSION, and release.json, removes consumed fragments, and opens
automation/release/vVERSION. It refuses a version not derived from the fragments.

## Publish

After that PR merges to main, release.yml:

1. validates one release identity across release.json, package metadata, runtime, and
   expected tag;
2. reruns tests and skill validation on the merge commit;
3. builds one wheel and one normalized sdist with commit-derived
   `SOURCE_DATE_EPOCH`, and requires rebuild equality in tests;
4. publishes through PyPI trusted publishing;
5. requires PyPI to expose exactly the built filenames and SHA-256 hashes;
6. creates vVERSION and the GitHub Release at the same commit;
7. dispatches an authenticated engineering-process-published event to renovate-ops.

The event carries the package, version, tag, publisher repository, and aggregate
distribution digest. renovate-ops accepts only the configured GitHub App sender and
then runs one repository-scoped Renovate job per explicitly enabled consumer.

Retries are identity-preserving. PyPI upload uses skip-existing only to resume; the
following exact hash comparison still fails on partial or conflicting content. A
draft GitHub Release can add only missing assets whose existing bytes already match;
a published release is never repaired or replaced. A tag on an older commit makes
later main pushes a no-op. A rerun on the exact release commit revalidates publication
and retries the idempotent adoption dispatch.

There are no release-plan review dispatches, authority transitions, evidence restore
chains, or separate publication controller. Branch protection, CI, independent
review, PyPI trusted publishing, and immutable GitHub release identity are the whole
boundary.
