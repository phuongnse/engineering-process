# Federated process improvement

This document owns the public feedback protocol that turns validated failures into
local correction or shared process evolution without transferring repository
authority.

## Ownership

A consumer owns its project behavior, exact evidence, local lifecycle, publication,
and adoption decisions. A process producer owns portable lifecycle semantics,
schemas, CLI enforcement, invariant classification, releases, and the public
improvement catalog. A transport moves immutable artifacts and may attest delivery;
it owns neither side's implementation, merge, release, nor adoption.

Consumer evidence is untrusted producer input. A signal can trigger triage but never
authorizes mutation. A producer disposition can classify and link work but never
authorizes implementation or delivery. A completed producer change does not prove an
immutable release or consumer recovery.

## Artifact chain

The portable chain contains four independent schema-1 JSON artifacts:

1. `engineering-process-improvement-signal` binds the consumer project, repository,
   checkpoint, process lock, trigger, owner claim, proposed invariant, bounded
   evidence hashes, and target producer. It contains no raw output, environment,
   secrets, credentials, or authority grant.
2. `engineering-process-improvement-disposition` is producer-owned triage. It binds
   the canonical signal digest, assigns the canonical invariant and reusable class,
   records new, duplicate, or recurring status, and links accepted work to a producer
   lifecycle. Rejection is explicit and evidence-backed.
3. `engineering-process-improvement-resolution` binds an accepted disposition to a
   completed producer lifecycle receipt and one immutable public release. It grants
   no consumer adoption authority.
4. `engineering-process-improvement-reproduction` binds the producer version, tag,
   release name, commit, and artifact-set digest to a clean consumer checkpoint, its
   selected-skill process-lock digest, and passing project-owned profiles. Only this
   artifact closes a shared consumer case.

Every later artifact carries canonical digests for every prior artifact. Validation
is bounded by item count, document bytes, aggregate bytes, identifier size, and URI
size. Core reads and writes local files only. GitHub issues, dispatches, MCP calls,
queues, or other delivery mechanisms are optional adapters and must preserve exact
artifact bytes.

## Lifecycle gates

A rejected governed verification profile or unresolved independent-review finding
creates a pending improvement case. Corrective work cannot silently advance through
another verification, completion, or source publication while classification is
missing.

Classification records:

- owner boundary: `project-local`, `shared-process`, `operations-or-external`, or
  `missing-product-or-authorization-input`;
- reusable class: local behavior, process rule, deterministic enforcement,
  portability gap, or obsolete guidance;
- canonical or proposed invariant id;
- explicit local, shared, external, input, or producer disposition;
- a rationale digest and, for shared escalation, the producer target.

A reviewed local fix may close at lifecycle completion. An accepted inbound producer
case may reach producer completion but stays release-owned until an immutable
resolution exists. A shared consumer case stays incomplete until disposition,
resolution, and reproduction all validate.

`processctl change status` reports local case blockers and next owner.
`processctl improvement status` reports a transported artifact chain without reading
either repository's private lifecycle files.

## Recurrence

`improvement-catalog.json` is producer-owned and versioned with the distribution. It
contains canonical invariant ids, reusable classes, public surfaces, status, and
public resolving change/release identity. It never contains consumer repository
identity or incident evidence.

A new signal assigned to a resolved catalog invariant is a recurrence. It cannot be
closed as another non-shared narrow incident unless the producer disposition carries
an explicit owner-approved exception and evidence digest. An active invariant yields
a duplicate disposition linked to existing work. A new semantic class is added only
through a reviewed producer lifecycle.

## Release and adoption

Producer completion, producer release, consumer adoption, and consumer reproduction
are separate boundaries:

1. Public N-1 governs and reviews the producer capability.
2. The owner separately authorizes one immutable producer release.
3. A consumer independently adopts that release under its currently pinned public
   authority. It never commits a dependency on the producer working tree.
4. The consumer reruns the exact affected boundary and exports reproduction evidence.
5. Aggregate improvement closes only when producer and consumer proof are both valid.

Before release, a read-only candidate artifact may provide forward compatibility
evidence at a real consumer. It is supplemental proof, not a production dependency,
adoption, publication, or merge authorization.

## Commands

~~~text
processctl improvement classify ...
processctl improvement observe ...
processctl improvement export-signal ...
processctl improvement disposition ...
processctl improvement ingest ...
processctl improvement resolution ...
processctl improvement reproduction ...
processctl improvement attach ...
processctl improvement validate-chain ...
processctl improvement status ...
~~~

All mutation commands operate only on the selected local project and explicit output
files. No improvement command performs network work or mutates another repository.
