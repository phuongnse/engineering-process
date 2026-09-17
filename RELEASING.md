# Releasing

A release has one authorization boundary: merge of a normal version-changing Release
PR after CI and independent review.

## Prepare

Every externally meaningful change adds one `release-changes/*.json` fragment with
`schemaVersion: 1`, a direct source reference, and complete problem/change/surface/
application/compatibility/notes details. If one pull request resolves multiple issues,
create one fragment per issue or independently adoptable behavior. Do not use an issue
range as the only description. The fragment type is `fix`, `capability`, or `breaking`;
the highest type derives the next version:

- fix: patch;
- capability: minor;
- breaking: major.

Run the Prepare release PR workflow with that exact version. It executes:

    python verification/prepare_release.py VERSION

The script validates every fragment, creates the current schema-version-1 `release.json` that preserves
all structured detail records, updates pyproject.toml, engineering_process.VERSION, and
RELEASE_NOTES.md, removes consumed fragments, and opens automation/release/vVERSION.
It refuses incomplete or non-current fragments and a version not derived from the
fragments.

For an owner-authorized release-tooling correction, run the same preparation command
in the candidate branch and review its code and generated version files together in
the normal Release PR. This retains the same CI, independent review, and merge boundary.

## Release contents

`release.json` is the sole contents authority. Each record explains the problem,
observable change, affected paths, application action, compatibility impact, and
important notes; source identifies one issue, PR, or owned change reference. A
breaking record names the required consumer action and compatibility impact.
Implementation identities and version-bump PR titles are not descriptions
of shipped features.

Preparation generates `RELEASE_NOTES.md`, grouped into breaking changes, features,
and fixes, with each issue source and the detail projection selected for its change
type. The manifest remains the review authority for complete detail fields. Detailed releases
also state explicitly when no breaking changes are included. Review this artifact
alongside the manifest. Regenerate it with
`python verification/render_release_notes.py --output RELEASE_NOTES.md`; local and
CI checks use `--check RELEASE_NOTES.md` to reject missing or stale bytes. Do not
maintain a second handwritten changelog.
The wrapper supplies consumer-owned records and upgrade text to the reusable renderer
under the selected `release-notes` standard. The same definition drives generation and
byte comparison; [consumer overrides](ARTIFACT_STANDARDS.md) remain repository-owned.
Record text is rendered as readable literal Markdown metadata: only structural syntax
is escaped, and ordinary owned references use normal inline-code delimiters. Ordinary
records emphasize result and compatibility; capability records add the consumer
action; breaking records include the problem and adaptation action. A release record
is not a substitute for a list of source issues; split independently adoptable issues
or behaviors into separate records.

## Publish

After that PR merges to main, publish.yml:

1. validates one release identity across release.json, package metadata, runtime, and
   expected tag;
2. reruns tests and skill validation on the merge commit;
3. builds one wheel and one normalized sdist with commit-derived
   `SOURCE_DATE_EPOCH`, and requires rebuild equality in tests;
4. publishes through PyPI trusted publishing;
5. requires PyPI to expose exactly the built filenames and SHA-256 hashes;
6. creates vVERSION and the GitHub Release at the same commit using the reviewed
   notes file, and checks the body before publication or resume;
7. dispatches an authenticated engineering-process-published event to renovate-ops.

The event carries the package, version, tag, publisher repository, and aggregate
distribution digest. renovate-ops accepts only the configured GitHub App sender and
then runs one repository-scoped Renovate job per explicitly enabled consumer.

The publisher checks actual JSON and Simple API filenames and hashes within its
bounded visibility loop. It does not infer consumer readiness from an elapsed cache
TTL. Consumers refresh process metadata at pip-compile and hash-locked installation
boundaries as described in README.md. A missing release or a hash mismatch remains
a failed installation; no delay or retry permits a different version or artifact.

Retries are identity-preserving. PyPI upload uses skip-existing only to resume; the
following exact hash comparison still fails on partial or conflicting content. A
draft GitHub Release can add only missing assets whose existing bytes already match;
a published release is never repaired or replaced. A tag on an older commit makes
later main pushes a no-op. A rerun on the exact release commit revalidates publication
and retries the idempotent adoption dispatch.

Historical release records use the contract that published them and are not runtime
inputs for the current release. Previously published release bodies are never
rewritten; a current body mismatch fails instead of silently replacing reviewed or
published text.
If a published release such as v2.5.0 needs clearer explanation, preserve its tag and
published body and make the correction through a later owner-authorized release or
the owner's documented publication policy.

There are no release-plan review dispatches, authority transitions, evidence restore
chains, or separate publication controller. Branch protection, CI, independent
review, PyPI trusted publishing, and immutable GitHub release identity are the whole
boundary.
