---
name: publish-change
description: Publish or update a branch, pull request, or equivalent review object after the engineering lifecycle completes. Use only when the user authorizes a remote publication action or asks to change publication metadata.
---

# Publish a Change

## Goal

Publish one approved immutable checkpoint through the project-owned remote workflow
without duplicating implementation, verification, or review.

## Workflow

1. Classify the exact authorized remote action and read the project publication
   policy and current lifecycle status.
2. For source publication, require a current completion record whose checkpoint and
   workspace fingerprint match the source being published.
3. Populate the managed PR-description sections with project-specific facts. Run
   processctl publication validation for branch, commit range, PR title, body, and
   draft/ready state; then run any stronger project-declared publication checks.
4. Perform only the authorized remote action. Preserve draft state unless ready state
   was explicitly requested.
5. Record the remote identifier and status. Treat any later source change as a new
   lifecycle cycle with invalidated publication readiness.

## Hard gates

- Never infer authorization to commit, push, open, merge, release, or deploy.
- Do not publish source with stale evidence or unresolved required findings.
- Do not replace, omit, or weaken the managed publication sections or standard
  requirements; projects may append stricter metadata and checklists.
- Metadata-only work may skip code implementation only when project policy permits it.

## Output

Return action, change id, checkpoint, completion evidence, metadata validation,
remote state, and blockers.
