---
name: run-project-command
description: Select, execute, and maintain reproducible project commands for setup, diagnosis, generation, testing, local development, verification, and continuous integration. Use when choosing an executable proof, changing command topology, or handling a blocked project prerequisite.
---

# Run a Project Command

## Goal

Use the smallest project-declared command that proves the required boundary and keep
repeatable execution deterministic.

## Workflow

1. Classify the moment as setup, diagnosis, exploration, implementation proof,
   lifecycle verification, publication, or continuous-integration reproduction.
2. Read `.process/project.json` and the nearest project owner. Reuse current evidence
   while its checkpoint, command, environment, and acceptance boundary remain valid.
3. Select the narrowest declared command. Use a broad profile only for cross-cutting
   invalidation, an inseparable dependency, or an explicit project requirement.
4. Execute argument arrays without a shell. Keep secrets out of arguments and
   evidence. Do not run commands concurrently when they share mutable build outputs.
5. On failure, preserve the exact command, exit status, environment, and missing
   prerequisite. Do not substitute another runtime or evidence boundary.
6. Add a reusable command or deterministic check only in its distribution owner;
   consumer manifests bind project data and commands but do not reimplement process.

## Hard gates

- Native read-only inspection is not verification evidence unless declared as such.
- Do not replace focused missing evidence with an unrelated broad suite.
- Do not install tools, change host trust, or mutate external state without authority.

## Output

Return moment, selected commands and reasons, results, reused evidence, omitted broad
checks, blockers, and next verification boundary.
