---
name: publish-change
description: Publish or update source and its remote review object after the engineering lifecycle completes. Use only when the user authorizes a remote publication action or asks to change publication metadata.
---

# Publish a Change

## Goal

Publish one approved immutable checkpoint through the project-owned remote workflow
without duplicating implementation, verification, or review.

## Workflow

1. Classify the exact authorized remote action and read the project publication,
   version-governance, and current lifecycle status.
2. For source publication, require a current completion record whose checkpoint and
   workspace fingerprint match the source being published.
3. Populate the managed review-description sections with project-specific facts. Run
   processctl publication validation for source name, change range, review title,
   body, and draft/ready state; then run any stronger project-declared publication
   checks. If the project declares repository governance, require its read-only
   provider check to confirm review-object-only integration, current stable checks,
   and no policy drift before integration or release. For each satisfied standard
   requirement, publish the referenceable evidence required by the current
   publication contract in the section that owns the claim.
4. Render public evidence for a reader who does not know the local orchestrator.
   Public evidence names the independent role, attested separation, review outcome,
   required-finding disposition, and exact immutable source. Machine actor aliases,
   context identifiers, cycle counters, local paths, and digests remain in a clearly
   labeled machine-verifiable record; they are audit fields, not public identities.
   Inside a review object, evidence omits the container number from headings, states
   the full checkpoint once, and does not use short hashes as decoration.
5. Perform only the authorized remote action. Preserve draft state unless ready state
   was explicitly requested.
   For a release, export and validate the completion receipt, derive the only
   permitted next version and every public identity from the release contract, then
   require the selected publication adapter to prove every declared surface matches
   before publication.
6. Record the remote identifier and status. Treat any later source change as a new
   lifecycle cycle with invalidated publication readiness.

## Hard gates

- Never infer authorization to create a source checkpoint, publish source, open or
  update a review object, integrate, release, or deploy.
- Do not publish source with stale evidence or unresolved required findings.
- Do not hand-edit one release identity surface independently of the release contract
  or update consumer locks before public artifact hashes are verified.
- Do not treat an automation-generated proposal as adoption evidence or allow a
  process-authority update to integrate automatically.
- Require one process-adoption review candidate to contain its complete dependency
  integrity, process lock, managed contract, and selected skill snapshots before
  review. Explicit integration ends adoption; never defer synchronization to a later
  step.
- Do not replace, omit, or weaken the managed publication sections or standard
  requirements; projects may append stricter metadata and checklists.
- Do not infer permission to create or update remote repository rules. A missing or
  drifted integration policy blocks publication until the repository owner separately
  authorizes a current compare-before-write plan.
- Do not present an internal actor alias, context id, local path, or unpublished digest
  as externally referenceable evidence. Publish the artifact through an authorized
  project-owned boundary first, then publish its stable review-object reference.
- Do not make machine lifecycle identity carry the public explanation. Public summaries
  are semantic; exact machine records remain labeled audit detail.
- Metadata-only work may skip code implementation only when project policy permits it.

## Output

Return action, change id, checkpoint, completion evidence, metadata validation,
remote state, and blockers.
