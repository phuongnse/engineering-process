# GitHub repository adapter

This document owns the GitHub-specific implementation of the portable contract in
[`REPOSITORY_GOVERNANCE.md`](REPOSITORY_GOVERNANCE.md). GitHub resource names,
permissions, workflow syntax, credential discovery, and API behavior stay here so
another hosted-repository provider can add a peer adapter without changing portable
policy.

## Workflow metadata

Workflow metadata is an observability surface, not decoration. Workflow filenames,
job IDs, step IDs, output IDs, and artifact-name segments use kebab-case. Human-facing
workflow, run, job, and step names use sentence case and state the owned boundary or
outcome. Generic labels such as `CI`, `Build`, `Verify`, `Check`, `Guard`, or `Gate`
are not sufficient by themselves.

Every workflow declares a meaningful `name` and `run-name`; every job and step
declares a meaningful `name`, including steps that invoke a reusable action. Matrix
job names include the platform and runtime dimensions that distinguish their
evidence. Environment variables use uppercase snake case and describe the value, not
the implementation step that happens to consume it.

## Credentials and permissions

The adapter reads credentials only from `GH_TOKEN` or `GITHUB_TOKEN`. Tokens never
belong in policy, plans, command arguments, output, or lifecycle artifacts. Read-only
inspection needs repository metadata, rules, pull-request, and checks access plus
enough authority for the API to disclose bypass actors; an omitted bypass list is
unverifiable and fails closed. Applying a plan additionally needs repository
Administration write permission.

## Check, plan, and apply

Create and validate portable policy without contacting GitHub:

~~~text
processctl repository init --project-root .
processctl repository validate --project-root .
~~~

Inspect live state without mutation:

~~~text
processctl repository github check \
  --project-root . \
  --repository OWNER/REPOSITORY
~~~

Before activation, both stable contexts must have completed successfully on the same
exact pull-request head. Planning reads that evidence, selects at most one
repository-owned ruleset targeting `~DEFAULT_BRANCH`, preserves stronger unrelated
rules, and writes a new exclusive plan:

~~~text
processctl repository github plan \
  --project-root . \
  --repository OWNER/REPOSITORY \
  --evidence-pr NUMBER \
  --output repository-governance-plan.json
~~~

If multiple default-branch rulesets exist and no unique managed owner can be selected,
planning fails rather than guessing. The plan binds repository identity, portable
policy, normalized current ruleset, desired payload, pull-request head, and successful
GitHub Actions check-run identities.

Application is a separate repository-owner action:

~~~text
processctl repository github apply \
  --project-root . \
  --plan repository-governance-plan.json \
  --confirm-repository OWNER/REPOSITORY
~~~

Immediately before `POST` or `PUT`, apply re-reads every bound input. Any mismatch
fails closed. The adapter never deletes a ruleset, creates a bypass actor, or mutates
live settings during bootstrap, synchronization, adoption, verification, review,
integration, or release.

## Workflow implementation

A GitHub consumer normally uses two workflows:

1. A lightweight `Change metadata policy` workflow listens for review-object creation,
   synchronization, reopening, metadata edits, and draft/ready transitions. It
   validates title, body, branch, commit range, and readiness using the installed
   public process authority.
2. Checkpoint verification runs on source synchronization and exposes one final
   `Merge eligibility` job. It uses an always-evaluate condition and fails unless
   every required upstream job or matrix succeeds. A skipped heavy job is acceptable
   only when successful project-owned impact selection proves it inapplicable.

Metadata edits refresh only `Change metadata policy`; a new source checkpoint runs
both workflows. The ruleset requires both contexts for the current head.

## Producer bootstrap

The producer adds and verifies this adapter through the public N-1 lifecycle but does
not use code under development to mutate GitHub. The current public authority—or an
owner applying the reviewed settings manually during the bootstrap window—owns
activation and read-back verification.
