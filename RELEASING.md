# Releasing engineering-process

Public releases are authorized by merging one generated Release PR. Every operation
after that merge is automated: immutable tag creation, draft preparation, distribution
build and verification, GitHub Release publication, PyPI publication through OpenID
Connect, and Renovate discovery for self and consumer adoption PRs.

Feature PR merge is not publication authorization. It contributes one bounded release
change fragment; only the separately reviewed Release PR merge authorizes publication.

The repository-root GitHub Action is published by the same immutable tag and commit
as the Python artifacts. It has no floating release channel or separately mutable
payload. Consumer workflows pin the full release commit and keep the corresponding
tag only as an annotation; their Python package pin and action update are grouped in
one reviewed adoption candidate after the initial clean cutover.

## One-time repository controls

Configure these controls before enabling the workflows:

1. Add the pending PyPI trusted publisher for project `engineering-process`, GitHub
   owner `phuongnse`, repository `engineering-process`, workflow `publish.yml`, and
   environment `pypi`. Do not store a PyPI password or API token.
2. Enable GitHub release immutability. It applies only to releases published after the
   setting is enabled.
3. Create the `pypi` environment and restrict deployments to protected `main`, the
   base-controlled recovery authority that independently revalidates the immutable
   release tag and every artifact before requesting OIDC. Do not add a separate
   required deployment approval: the protected Release PR merge is the sole release
   authorization.
4. Allow GitHub Actions to request the workflow-declared write permissions and to
   create pull requests. The release bot never uses approval authority.
5. Protect `main` and require the complete CI matrix, the `release-authorization`
   commit status for Release PRs, current branch state, and an approving independent
   review. Dismiss stale approvals when the head changes.
6. Permit only the repository release workflow to create `refs/tags/v*`; block tag
   deletion, update, and force-push. The selected protected merge method must produce
   the identical reviewed Git tree; authorization binds both the reviewed head and
   resulting release commit without assuming a particular commit ancestry shape.
7. Keep `automation/release/next` bot-owned. Humans review and merge its PR but never
   push release content directly to protected `main`.

## Release change fragments

Every distributable feature or fix adds one `release-changes/<id>.json` document using
`schemas/release-change.schema.json`. The fragment declares `fix`, `capability`, or
`breaking`, its sorted public surfaces, rationale, schema impact, and required breaking
migration guidance. A change with no distributable public impact adds no fragment and
does not advance the package version.

After a fragment reaches `main`, `release-pr.yml` resets the bot-owned release branch
to that protected checkpoint and runs:

~~~text
processctl publication prepare-release
~~~

The command consumes every bounded fragment, derives the exact next SemVer, and updates
`release.json`, `pyproject.toml`, `engineering_process.VERSION`, tag, title, wheel,
sdist, evidence, and attestation identities together. It also generates the N-1
lifecycle contract and plan under `.release/`. The workflow force-updates only the
bot-owned branch with an exact force-with-lease and creates or refreshes one
Release PR. A new feature fragment invalidates the old head, evidence, status, and
review before updating that PR.

[`VERSIONING.md`](./VERSIONING.md) is the normative classification policy.
`processctl publication plan-version` is available for preview, but the generated
Release PR is the only version-bearing checkpoint.

## Pre-merge release qualification

Every source PR with pending release fragments must pass one disposable end-to-end
qualification on the designated Linux/Python job. CI installs the exact public N-1
authority from `requirements/process.txt` with hashes, clones the immutable source
checkpoint without credentials, materializes and commits the next release candidate,
then requires N-1 to complete contract and plan validation, implementation
registration, both verification profiles, review assignment, review submission,
finish, evidence export, and evidence validation.

The review report derives its schema, quality dimensions, statuses, and acceptance
criteria from the registered immutable release contract. A producer test alone is not
sufficient for a lifecycle schema change: the released consumer must accept every
generated artifact at the last transition that reads it.

Qualification evidence is synthetic, labelled qualification-only, kept inside the
deleted temporary clone, and never uploaded or reused. The job has no tag, release,
package publication, dispatch, PR-write, or production authorization path. The actual
independent verifier and exact-head authorization still run on the generated Release
PR. A source PR cannot be treated as release-ready merely because unit tests pass.

## Release authorization

`release-candidate.yml` always reports a stable required check. For an internal Release
PR it checks out the exact head with read-only repository permissions, installs the
hash-locked public N-1 authority, runs both required lifecycle profiles, starts an
isolated agent-review assignment bound to the immutable `renovate-ops` verifier, and
preserves the review-pending state as a bounded Actions artifact. Other PRs receive an
explicit not-applicable success instead of a skipped required check.

The Release PR initially carries pending managed statuses. After CI succeeds,
`release-approval.yml` runs from protected default-branch workflow code with exact
inputs dispatched by the completed CI run. It validates the exact PR head, downloads the read-only external
verification report, requires the pinned verifier repository and commit, restores the
exact successful candidate lifecycle, submits the approved agent report, completes it,
and exports its evidence. It then marks the managed checklist satisfied and sets
`release-authorization` on that exact SHA. No separate human review is manufactured;
the explicit merge by the sole maintainer is the human authorization.

Merging that ready Release PR is the only publication authorization. No maintainer
runs a release command, creates a tag, creates a GitHub Release, uploads an asset, or
approves a second deployment gate after merge.

## Automated publication

`release.yml` handles only a merged internal `automation/release/next` PR. A serialized
job restores the exact-head authorization artifact, requires the successful status,
and proves that the reviewed head and release commit have the identical Git tree. It
validates the release contract against the latest reachable final SemVer
tag and protected `main` before creating anything.

The workflow then performs this deterministic state machine:

1. Create the absent `v<SemVer>` tag at the exact merge commit, or require an existing
   tag to point there. A tag is never moved.
2. Create the absent draft GitHub Release with the same tag and title, or require the
   existing release identity to match.
3. Attach the exact authorization evidence. An existing byte-identical asset is reused;
   a conflicting asset fails closed.
4. Call `prepare-release.yml` as a reusable workflow. It builds wheel and sdist from
   the tagged commit, binds their bytes and authorization evidence in the distribution
   attestation, installs and tests the wheel, and uploads only absent or byte-identical
   assets. The complete asset set must match `release.json` exactly.
5. Publish the fully prepared immutable GitHub Release, then use the short-lived
   GitHub App to send one exact `engineering-process-release-ready` event back to the
   repository. GitHub runs top-level trusted workflow `publish.yml` from protected
   `main`; this does not rely on an event generated by `GITHUB_TOKEN` and preserves the
   workflow identity registered with PyPI.
6. `publish.yml` validates the App sender and bounded tag, commit, version, and
   attestation payload. It then downloads and verifies GitHub's immutable release and each asset,
   independently revalidates source, evidence, tag, ancestry, artifact names, hashes,
   attestation, and the installed wheel in both the no-OIDC build job and the gated
   PyPI job. It accepts an existing PyPI version only when the complete filename,
   size, and SHA-256 set is identical; an absent version uploads wheel and sdist with
   PyPI attestations through OIDC and waits for both the exact version JSON and the
   Simple API file hashes used by pip consumers before dispatching adoption.
7. After exact PyPI verification, a short-lived GitHub App token sends one bounded
   `engineering-process-published` event to `renovate-ops`. The event starts the
   allowlisted production adoption scan; no release polling schedule participates.

Retries are permitted only against the same tag, commits, names, and bytes. A complete
exact PyPI version is idempotent success; a partial or conflicting version fails
closed. Never enable `skip-existing`, replace a PyPI file, move a tag, or mutate a
published release. A semantic defect or partial PyPI publication is corrected under a
newly derived version. A transient retry may resume an unpublished draft or an exact
published GitHub Release when every existing byte matches.

## Bootstrap authority

Public `0.1.1` is `bootstrap-history` and cannot export a governed lifecycle receipt.
The first generated Release PR therefore uses schema-3 mode `bootstrap-authority` and
a separately typed `engineering-process-bootstrap-authorization` bundle. It never
claims to be a lifecycle receipt. The protected Release PR checks, independent review,
tree-equivalence proof, immutable assets, and OIDC publication remain mandatory.

Only a release whose latest predecessor is bootstrap history may use this mode. After
that release reaches PyPI, its Renovate self-adoption PR must pin the public artifact,
complete hash graph, process lock, and managed assets before another Release PR can be
prepared. Every subsequent release is `governed` and must carry a lifecycle receipt
exported and validated by the pinned public N-1 authority. A second bootstrap attempt
or a governed release under a stale process lock fails closed.

## Adoption

Successful exact PyPI verification sends an authenticated bounded event to the
self-hosted Renovate control plane. The existing exact-pin, pip-compile, and managed
adoption runner contract then creates or updates the self-adoption draft and every
opted-in consumer draft. Event delivery is at least once and Renovate is idempotent;
there is no scheduled production poll. Those PRs contain the full lock graph, managed
assets, and any consumer-owned target-version migration.

The first release containing the shared CI install action is followed by one bounded
cleanup PR per existing consumer: pin the action's full release commit, replace local
installer invocation, and delete the copied installer and its algorithm unit tests in
the same checkpoint. Later releases update the action and Python identities together
through Renovate. Managed adoption runners remain byte-verified distribution
snapshots because they bootstrap the target authority inside Renovate; they are not
consumer-owned implementations and are not replaced by an unpinned network command.

The Renovate host must allow only the literal managed runner command. For self-hosted
Renovate, configure the administrator-owned `allowedCommands` value, never repository
config, with this anchored expression and leave shell execution disabled:

~~~json
{
  "allowedCommands": [
    "^python \\.process/adopt-process\\.py --project-root \\. --requirements-lock requirements/process\\.txt$"
  ]
}
~~~

For Mend-hosted Renovate, command execution requires a host-side allowlisting grant;
confirm the effective `allowedCommands` entry in Renovate logs. Without that grant,
the package-only update is incomplete and must not be merged.

Adoption PRs never auto-merge by default. Each repository requires its own CI and
independent review, then explicitly merges the exact candidate. A consumer may opt in
to continuous adoption through its own repository policy; the publisher never infers
consumer policy or grants itself consumer merge authority.

## Release reliability invariants

The following rules are regression contracts, not bootstrap exceptions:

- New lifecycle documents use the current schema required by public N-1, while
  historical readers remain available. Every paired producer/consumer path is tested
  together through completion before merge.
- The producer self-adopts the latest public authority before preparing the next
  version. If self-adoption is stale, automation defers version derivation and
  redispatches the exact current release through the idempotent publish/adoption path;
  it does not invent another version or require a manual bridge.
- The process hash lock is generated with a pinned lock generator and contains hashes
  for the complete supported Python/OS binary matrix, not only the machine that
  generated it.
- Registry readiness means both the PyPI version JSON and Simple API expose the exact
  expected filenames and hashes. Adoption is not dispatched from an eventually
  consistent partial view.
- Remote GitHub asset reads use bounded retry only for the same immutable tag, names,
  sizes, and hashes. A conflicting byte is never retried into acceptance.
- Release recovery uses the exact unexpired pre-publication completion artifact when
  it exists. Only after the declared release is published and GitHub verifies it as
  immutable may recovery restore the same declared evidence asset from that release;
  an absent, draft, mutable, mismatched, or incomplete release remains fail-closed.
- The Renovate control plane requires a terminal successful result for every
  allowlisted repository and rejects artifact errors or partial scans. Event delivery
  is App-authenticated and idempotent; cron polling and maintainer-run bridge commands
  are not production dependencies.
