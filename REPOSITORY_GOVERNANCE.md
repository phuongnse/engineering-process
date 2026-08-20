# Repository governance

Repository governance is the external integration gate between an approved source
checkpoint and the default branch. It supplements the engineering lifecycle; it does
not replace verification, independent review, merge authorization, or release
authorization.

## Portable baseline

Every governed repository declares `.process/repository-governance.json`. Schema 1
requires the default branch to:

- accept changes only through a pull request or provider-equivalent review object;
- block deletion and non-fast-forward updates;
- forbid bypass actors;
- require the stable `Change metadata policy` and `Merge eligibility` contexts;
- declare whether required checks must be refreshed after the branch falls behind the
  default branch.

Projects may add stronger checks or a separate stronger ruleset. They cannot remove
or rename the two stable contexts. `Change metadata policy` validates current review
metadata and publication policy. `Merge eligibility` succeeds only when all
project-owned verification for the immutable head checkpoint succeeds. Individual
matrix and domain job names remain project-owned implementation details behind
`Merge eligibility`; copying all of them into remote settings creates avoidable
configuration drift.

A protected branch prevents merging a checkpoint whose required checks are red or
missing. It cannot make a flaky check reliable, and it cannot prove that a later
post-merge rerun will have the same result. Reliability defects still require their
own reproducer, fix, and regression evidence.

## Workflow metadata contract

Workflow metadata is an observability surface, not decoration. Provider integrations
use kebab-case for filenames, job IDs, step IDs, output IDs, and artifact-name
segments. Human-facing workflow, run, job, and step names use sentence case and state
the owned boundary or outcome. Generic labels such as `CI`, `Build`, `Verify`,
`Check`, `Guard`, or `Gate` are not sufficient by themselves.

Every workflow declares a meaningful `name` and `run-name`; every job and step
declares a meaningful `name`, including steps that invoke a reusable action. Matrix
job names include the platform and runtime dimensions that distinguish their
evidence. Environment variables use uppercase snake case and describe the value,
not the implementation step that happens to consume it. Required-check context names
are public policy identifiers and change only through a governed migration.

## GitHub adapter

The optional GitHub adapter reads credentials only from `GH_TOKEN` or `GITHUB_TOKEN`.
Tokens never belong in a policy, plan, command argument, output, or lifecycle
artifact. Read-only inspection needs repository metadata, rules, pull-request, and
checks access plus enough repository authority for the API to disclose bypass actors;
an omitted bypass list is unverifiable and fails closed. Applying a plan additionally
needs repository Administration write permission.

Create and validate a policy without contacting GitHub:

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

Before activation, both required contexts must have completed successfully on the
same exact pull-request head. Planning reads that evidence, selects at most one
repository-owned ruleset targeting `~DEFAULT_BRANCH`, preserves stronger unrelated
rules, and writes a new exclusive plan file:

~~~text
processctl repository github plan \
  --project-root . \
  --repository OWNER/REPOSITORY \
  --evidence-pr NUMBER \
  --output repository-governance-plan.json
~~~

If multiple default-branch rulesets exist and no unique managed owner can be selected,
planning fails rather than guessing. A plan records the repository identity, policy
digest, current normalized ruleset digest, desired payload digest, pull-request head,
and successful GitHub Actions check-run identities.

Application is a separate repository-owner action:

~~~text
processctl repository github apply \
  --project-root . \
  --plan repository-governance-plan.json \
  --confirm-repository OWNER/REPOSITORY
~~~

Immediately before POST or PUT, apply re-reads repository identity, ruleset state,
policy, pull-request head, and latest check runs. Any mismatch fails closed. The
adapter never deletes a ruleset, never creates a bypass actor, and never mutates live
settings during bootstrap, sync, adoption, verification, review, merge, or release.

## Workflow ownership

The consumer owns workflow commands and domain jobs. A GitHub consumer normally uses
two workflows:

1. A lightweight `Change metadata policy` workflow listens for `opened`, `synchronize`, `reopened`,
   `edited`, `ready_for_review`, and `converted_to_draft`. It validates title, body,
   branch, commit range, and readiness using the installed public process authority.
2. Checkpoint verification runs on source synchronization and exposes one final `Merge eligibility`
   job. That job uses `if: always()` and fails unless every required upstream job or
   matrix completes successfully. A skipped heavy job is acceptable only when the
   project-owned impact contract explicitly makes that job inapplicable and the
   stable gate evaluates the resulting dependency state.

Separating these workflows lets metadata edits refresh `Change metadata policy` without rerunning
an expensive platform matrix. On a new source checkpoint, both workflows run and the
remote ruleset requires both current contexts.

## Rollout order

Repository protection is intentionally not self-activating:

1. Add the policy and stable workflows through the repository's existing authorized
   integration path.
2. Observe successful `Change metadata policy` and `Merge eligibility` checks on one
   exact review head.
3. Run read-only policy validation and create a plan bound to that evidence.
4. Obtain separate repository-owner authorization for the external settings write.
5. Apply the current plan, read the ruleset back, and require the check command to
   pass.
6. Only then rely on the ruleset as a merge or release prerequisite.

The producer follows the same order. Source under development can add and verify the
contract and workflows, but it does not use itself as authority to mutate GitHub.
The current immutable public authority—or an owner executing the reviewed exact
settings manually during the bootstrap window—owns activation.

An existing consumer with per-job required contexts migrates only after adopting the
immutable process release. It adds the two stable gates, maps every project-owned job
to `Merge eligibility` under its own workflow contract, observes both contexts on one exact
consumer PR, and then plans an update of its unique existing default-branch ruleset.
The consumer owns which dependency results may be `skipped`; each exception must be
derived from its successful impact-selection job. Consumer source and settings never
depend on an uncommitted producer checkout.
