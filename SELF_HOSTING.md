# Self-hosting engineering-process

This repository is both the producer of the engineering-process distribution and a
consumer of its lifecycle. It uses a staged trust chain so code under development
cannot approve itself.

## Trust chain

Release N governs the specification, plan, implementation registration, verification,
independent review, finding loop, and completion of release N+1. The lifecycle CLI is
installed from the exact public wheel pinned by `requirements/process.txt` and
`.process/process.lock`. Producer tests import the checkout under test, but lifecycle
state transitions are executed by the installed N distribution.

The two skill trees have distinct ownership:

- `.agents/skills` is the managed N copy used by agents working in this repository.
- `process_assets/skills` is the editable N+1 source packaged for future consumers.

Changing the source tree cannot change the instructions or enforcement governing the
current lifecycle cycle. After N+1 is published and its public hashes are verified, a
separate change advances this repository's lock and managed tree to N+1.
If N+1 activates project-owned capability configuration, that same self-adoption
change carries the target-version migration and updates `.process/project.json`
inside the adoption transaction. N+1 governs only changes opened after the complete
adoption checkpoint is reviewed and merged.

## Initial bootstrap root

Self-hosting began from commit `5055d37dc4d421ac97e9bf2329b56c6a2a69d5eb`
using public release `0.1.1`. The trusted wheel SHA-256 is
`3211775274a05569e006daae7e026f34295df9da2b2244f464f08aee00352f4f`, and the
selected full-distribution digest is
`sha256:73a6d3714ced574a4e85b3317bd713ee3fe0c08055ee154514706ae7eeb71603`.
The installed authority resolved outside the checkout and `processctl doctor` passed
for the producer's development profile before change
`self-hosted-impact-engine` was registered.

This one-time bootstrap establishes the root of trust; it is not a recurring bypass.
All later source changes require the normal `.process` lifecycle and a clean immutable
checkpoint. Verification artifacts under `.process/runs` bind the local checkpoint
and workspace fingerprint. GitHub checks, immutable release attestations, and PyPI
artifact hashes provide durable publication evidence.

The lifecycle state is the executable change specification and evidence ledger: it
binds the accepted contract and plan digests, implementation identities, required
profile reports, immutable checkpoint and workspace fingerprint, independent review
assignment and report, carried finding resolutions, and completion artifact. A dirty
or different checkpoint invalidates verification and approval instead of inheriting
stale evidence.

`.process/runs/` is durable local evidence, not a temporary directory. Active and
failed runs are retained for recovery. A completed run may be pruned only through
`processctl evidence prune --apply` after a bounded portable receipt has been
exported and independently validated; release receipts remain attached to the
immutable public release. Pruning first quarantines the exact run directory and
deletes it. If deletion fails, the possibly partial quarantine remains explicitly
named for inspection and the validated external receipt remains the recovery
authority; the implementation never renames a partial tree back as a complete run.

The isolated public N environment and build/impact temporary directories have a
single coordinator owner. Build and impact directories are removed on success,
failure, timeout, and interruption. The N environment is removed only after the final
N-governed completion/release transition; its version, wheel hash, process digest,
and exported receipt remain as durable provenance.

## Release boundary

Lifecycle completion proves engineering readiness only. It does not authorize a
version bump, tag, merge, publication, consumer lock update, or deployment. Those
actions follow `RELEASING.md` and require their own explicit authority.
At that boundary, `release.json` adds a machine-validated SemVer and compatibility
specification; the publication gate binds it to the exact latest public predecessor,
canonical GitHub title/tag/package/runtime/artifact identity, exported N-1 receipt,
source checkpoint, and `main` ancestry.
