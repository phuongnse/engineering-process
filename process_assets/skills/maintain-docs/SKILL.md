---
name: maintain-docs
description: Maintain durable documentation, diagrams, links, status text, routing, and ownership without duplicating product or process decisions. Use when guidance changes, when renamed or retired concepts may drift, or when documentation consistency must be verified.
---

# Maintain Documentation

## Goal

Keep one authoritative owner per fact and make every dependent document point to it.

## Workflow

1. Classify the content as product behavior, architecture, process, enforcement,
   operations, navigation, or generated output.
2. Classify its abstraction layer. High-level policy owns outcomes, invariants,
   ownership, and failure semantics; public contracts own stable interfaces; adapters
   and implementation guides own provider, platform, command, workflow, and serialized
   representation details.
3. Locate the project-declared owner. Preserve the domain decision supplied by that
   owner; documentation maintenance does not invent it.
4. Edit the owner once, replace duplicate rules with links, and remove stale names,
   session history, temporary status, and superseded instructions.
5. Move details that cross upward into their lower-level owner and leave only the
   portable outcome and link at the higher layer. Never copy an implementation detail
   upward merely to make a document self-contained.
6. Update diagrams or generated documentation from their declared source rather than
   editing derived output independently.
7. Run the smallest documentation checks declared by the project verification
   profiles, including any abstraction-boundary regression. Validate changed links and
   anchors when their targets moved.
8. For high-level policy, obtain independent semantic review that every concrete
   mechanism remains with its declared owner. Structural checks prove registration,
   dependency direction, and document shape; they do not infer the meaning of prose.
9. Apply the retirement and abstraction sweep and report unresolved ownership,
   layering, or generation gaps.

## Hard gates

- Do not move product, architecture, or release authority into a documentation skill.
- Do not put provider, platform, source-control, workflow, or serialized-layout
  mechanisms in a high-level standard.
- Do not use a blacklist of current technology names as the abstraction boundary.
- Do not infer a consumer compatibility, deployment, migration, or retirement
  strategy from an example, evaluation fixture, or implementation.
- Do not keep approval provenance or conversational history as durable policy.
- Do not duplicate a reusable process rule in a consumer project.

## Output

Return the owner changed, duplicates removed, generated artifacts, retirement sweep,
verification evidence, and unresolved ownership decisions.
