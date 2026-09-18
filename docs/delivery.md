# Deliver a change

Use this page when you are doing work in a consumer repository. It is the
reader-oriented route; the phase skills contain the detailed rules for each
operation, and processctl change status is the state authority.

## Before you start

Read the nearest AGENTS.md, the consumer's project configuration, and the
accepted request. Then run:

    processctl project validate --json

Record the consumer incident or request, choose a deliberate comparison reference,
and find the consumer's documentation entry point. During this inspection, answer
one question: after this change, what will a user, operator, developer, or
maintainer need to know or do differently? The answer can be “nothing”; it must
not be guessed from file names or reduced to a checkbox.

Start through the managed entry skill:

    deliver-change

It reads current state and routes exactly one phase. The six phase skills are:

| Phase | Main question | Evidence |
| --- | --- | --- |
| Start | Is the request accepted and bounded by real consumer evidence? | Contract, readiness, comparison base |
| Plan | What paths, causal mechanism, risks, docs sources, and evidence prove it? | Plan digest and invariant assessments |
| Implement | Does the candidate satisfy only that accepted plan? | In-scope diff and implementation identity |
| Verify | Do required consumer profiles pass on this exact candidate? | Current, identity-bound profile reports |
| Review | Can an independent reader and reviewer understand and use it? | Fresh assignment, findings, dispositions |
| Finish | Is the approved result durably recorded? | Receipt and cleanup state |

## Commands

The route is intentionally explicit:

    processctl contract validate --kind change change.json
    processctl change start --actor ACTOR --context CONTEXT --contract change.json
    processctl change plan --change-id ID --actor ACTOR --context CONTEXT --plan plan.json
    processctl change implement --change-id ID --actor ACTOR --context CONTEXT
    processctl change verify --change-id ID --profile development
    processctl change verify --change-id ID --profile review
    processctl change review start --change-id ID --actor REVIEWER --context REVIEW_CONTEXT
    processctl change review submit --change-id ID --review .process/runs/ID/review-1.json
    processctl change finish --change-id ID --actor ACTOR --context CONTEXT

Use the actual nextAction from:

    processctl change status --change-id ID --json

Before executing remaining verification, inspect:

    processctl change explain --change-id ID
    processctl change verify --change-id ID --remaining

evidence.requirements is the current decision. satisfied is reusable evidence;
remaining needs execution; unknown lacks a trustworthy input identity; blocked
needs a consumer or owner action; and inapplicable is an unselected optional
profile. A historical passed report alone is not a current pass.

## Documentation belongs inside the phase work

The ordinary documentation decision follows the same six phases:

1. **Start:** identify affected readers, find the current authoritative consumer
   source, and include a documentation outcome only when the accepted behavior
   changes durable knowledge.
2. **Plan:** name the source and any generated output in the existing work items.
   Keep the boundary literal. A plan may say that no documentation changes are
   needed and explain the reader consequence.
3. **Implement:** update the source a future reader will use. Regenerate derived
   output from its source. Do not add a page merely to report that work was done.
4. **Verify:** run the consumer's normal checks plus relevant link, rendering,
   example, or documentation checks. A green format check proves only format.
5. **Review:** follow a representative reader path without adding context from
   the implementation chat. Check correctness, findability, and ability to act;
   do not block on a preference about folders or headings.
6. **Finish:** keep the durable knowledge in consumer-owned files. The PR, review
   report, receipt, and runtime artifacts are evidence, not the documentation
   source.

The detailed decision examples are in
[Documentation quality](documentation.md). Process-owned guidance follows the
same rule: edit its source skill or standard, then verify the generated or
adopted surface at the relevant package boundary.

## State, recovery, and review

Status should tell a reader the phase, cycle, candidate checkpoint, contract/plan
identity, evidence state, review state, blocker, and next command. Do not open
run.json first unless status points to a detail that needs it.

If a candidate path is outside the frozen plan, stop. Do not widen a directory or
rewrite the plan; the owner must approve a superseding contract with
supersedes.reason set to missing-plan-boundary when the accepted outcome is
unchanged. Prior evidence and approval do not carry into the new run.

A failed required command is not permission to retry the same input through
--remaining. Inspect the safe diagnostic, change the consumer input or request
an explicit refresh, and keep the failure distinct from a later pass. Timeout,
output, stream, spawn, and cleanup failures are execution conditions, not invented
test diagnoses.

The reviewer is a genuinely independent actor and context for the current cycle.
The reviewer reads the complete diff, accepted contract, plan, exact profile
evidence, readiness, and relevant documentation sources, then authors and submits
the report. A coordinator cannot replace an independent verdict with prose.

## Handoffs and completion

Move an active run between workspaces only with the explicit handoff package after
the candidate is committed:

    processctl change handoff export --change-id ID --output HANDOFF_PATH
    processctl change handoff import --handoff HANDOFF_PATH

Finish writes one bounded completion receipt before removing only that change's
runtime. If cleanup is pending or failed, retry change finish; do not recreate
the change or treat the receipt as clean prematurely. Merge, deployment, release,
and final consumer adoption remain owner-controlled.
