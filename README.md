# Engineering Process

An agent-neutral, end-to-end engineering lifecycle. A consumer supplies project
policy and commands; this repository supplies the same specification, planning,
implementation, verification, independent-review, finding-loop, and completion
gates to every project.

The enforcement boundary has three parts:

1. Portable Agent Skills tell any compatible agent how to perform each phase.
2. `processctl` owns lifecycle state, transitions, immutable evidence, and exit codes.
3. The consumer's `AGENTS.md` and `.process/project.json` own domain policy and exact
   argument-array verification commands.

Publication conventions are distribution-owned as well: manual and automation branch
names, Conventional Commit subjects, PR titles, the managed PR-description structure,
structured requirement statuses, and draft-versus-ready semantics are validated by
`processctl publication ...`. Projects populate those sections with their own contract,
impact, risk, evidence, and review details and may append stronger domain checks.

Core semantics never name a model, agent product, orchestration API, or code-indexing
provider. An agent host or human workflow supplies an independent reviewer identity;
`processctl` rejects any reviewer actor or context used by the current implementation
cycle. If the host cannot attest separation, review remains blocked.

The core ships only the agent-neutral reviewer-attestation contract. Host-specific
launchers and model configuration are separate integrations and are never part of a
required process bundle.

This repository follows the same lifecycle it distributes. The exact public N-1
release pinned in `.process/process.lock` governs development of N+1; the checkout
under test never supplies its own lifecycle authority. Managed N-1 skills live in
`.agents/skills`, while editable N+1 distribution sources live in
`process_assets/skills`. The bootstrap trust chain and evidence boundary are defined
in [`SELF_HOSTING.md`](./SELF_HOSTING.md); package, schema, release, and adoption
versions are governed by [`VERSIONING.md`](./VERSIONING.md).

Python 3.11 or newer and Git are required. Windows command containment requires
Windows 10 or Windows Server 2016 and newer so Job Object membership can be attached
atomically during process creation. Lifecycle state is stored under ignored
`.process/runs/`; completion, review, and verification are bound to a clean Git
checkpoint and workspace fingerprint.

## Execution architecture

Validated failures participate in a federated improvement loop. Consumers export
bounded, redacted signals to the owning producer without granting authority;
producer triage, lifecycle completion, immutable release, consumer adoption, and
consumer reproduction remain separate gates. `processctl improvement status` exposes
the portable chain and next owner. See
[PROCESS_IMPROVEMENT.md](PROCESS_IMPROVEMENT.md).

Consumers use one foreground-task contract on every supported platform. The contract
owns argument-array commands, non-interactive standard input, bounded output, timeout,
exit status, and descendant cleanup. Platform selection occurs once inside the
distribution: the POSIX backend owns a new process session/group and the Windows
backend owns a kill-on-close Job Object. Consumer manifests, evidence, and exit codes
do not branch by operating system. If an outer Windows Job applies incompatible
nesting or UI limits, target creation fails closed instead of running uncontained.
After a command root exits, both backends allow at most 250 milliseconds for child
accounting to drain naturally. A process still present after that bound is terminated
and makes the command fail; commands with no remaining child return immediately.

This task boundary intentionally separates finite commands from services and
interactive protocols. `processctl exec`, requirement probes, setup command actions,
and verification checks are finite foreground tasks. Detached Docker Compose stacks,
log followers, interactive shells, watchers, and stdio servers remain project-owned
commands outside this executor until a separate service or interactive lifecycle is
specified. They must not be placed in verification profiles or wrapped by
`processctl exec`.

A finite command succeeds only when its process boundary passes and its complete
admitted stdout and stderr are free of classified warning and error diagnostics. The
shared classifier is bounded and non-configurable. Exit-zero diagnostics therefore
fail `doctor`, `setup`, `exec`, verification, and internal distribution commands at
the shared owner. Reports retain redacted diagnostic metadata and line digests, not
raw matched text. Fix the warning or error at its owner; do not silence it, replace
the canonical command, or set a tool-specific suppression variable to obtain a pass.

## Consumer bootstrap

Add only project-owned configuration:

~~~text
project/
├── AGENTS.md
├── .github/
│   └── PULL_REQUEST_TEMPLATE.md
├── .gitignore             # includes .process/runs/
└── .process/
    ├── adopt-process.py             # hash-locked adoption runner
    ├── adopt-process-windows-job.py # Windows process containment
    └── project.json                 # profiles and lifecycle baseline
~~~

Install `processctl` from a tagged release and create a candidate manifest from
`examples/project.json` with the repository's real commands. Bootstrap the complete
standard in one command:

~~~text
python -m pip install "engineering-process==0.1.1"
processctl project init --project-root . --manifest project.json \
  --bundle core --bundle delivery --bundle product
processctl doctor --project-root .
~~~

`project init` validates the manifest, preflights ownership conflicts, writes the
lock, installs the managed `AGENTS.md` and pull-request contracts, adds the ignored
lifecycle-state path and canonical managed-skill Git attributes, and synchronizes
the selected skills and adoption runner. It refuses to replace differing project
configuration or unmanaged skills unless the conflict is resolved explicitly. `sync --check` and
`doctor` detect drift in skills, the managed agent contract, the pull-request block,
and the bounded process-owned `.agents/.gitattributes` file. That file is closer to
the managed tree than project-root attributes, canonicalizes LF only for text assets
under `.agents/skills`, and disables inherited working-tree encoding, filter, and
ident transforms for those assets. A self-rule applies the same byte-stable policy
to `.agents/.gitattributes`; binary detection remains automatic. Deeper repository
attribute files are rejected by existing managed-tree ownership and content checks.
External Git overrides that alter a checkout still fail byte-exact distribution
attestation. A consumer never authors or maintains process skills locally.

CI installs the pinned authority through the repository-root
`phuongnse/engineering-process` action from the same governed release. Consumers pin
the action with the release commit's full object id and retain the human-readable
`v<SemVer>` annotation; floating tags and copied installer implementations are not
supported. The action reads the consumer-owned `requirements/process.txt` and changes
no version or source decision: it preserves the complete hash lock, public PyPI,
binary-only policy, sanitized pip environment, exact-version-only propagation retry,
bounded output and time, and cross-platform descendant cleanup. The action source is
resolved only from `github.action_path`, so an untrusted consumer checkout cannot
replace the installer.

Project-owned CI workflows remain local because they select the project's commands
and evidence. Reusable installation and publication grammar belong to this
distribution; product, architecture, dependency, documentation, and acceptance
checks remain with the consumer. The managed `.process/adopt-process.py` and Windows
Job Object sidecar are intentional bootstrap snapshots, not consumer implementations:
Renovate must install and verify a target authority before that target exists in the
checkout, and `processctl sync --check` compares those bytes with the pinned
distribution.

For an existing consumer, automation may prepare one unpublished adoption candidate,
but the normal Renovate PR-first route is excluded from process-authority updates.
The managed runner installs the target authority from the complete hash lock outside
the checkout and atomically updates the process lock and managed assets before the
lifecycle host publishes the completed candidate. If the consumer chooses or requires
new project configuration, it adds
`.process/adoption-migrations/<target-version>.json`; the installed target authority
binds the source and target manifest digests, validates the complete target manifest,
and updates `.process/project.json` in the same rollback transaction. Optional
capabilities are never inferred. CI and a fresh isolated review context approve the
fully materialized checkpoint; merge completes adoption and no post-merge sync runs.

The engineering-process producer repository separately owns its root
`.gitattributes` policy so tracked text sources and distribution inputs are LF and
byte-stable on every supported checkout. That producer policy is not synchronized
into a consumer root; consumers receive only the bounded `.agents/.gitattributes`
asset described above.

The single project-manifest contract includes environment profiles, project-attested
read-only requirement probes, remediation, declarative managed-tool artifacts, and
optional setup actions. Use the same interface in every consumer:

~~~text
processctl doctor --project-root . --profile development
processctl setup --project-root . --profile development
processctl setup --project-root . --profile development --apply \
  --allow network --allow user-files --allow project-files
processctl exec --project-root . --profile development -- \
  python scripts/project.py local-dev
~~~

A portable tool is data, not a consumer-owned installer. Each project pins the
version and one immutable artifact contract per supported platform, then references
the tool from a `managed-tool` setup action:

~~~json
{
  "managedTools": [{
    "id": "sample-tool",
    "version": "1.2.3",
    "artifacts": [{
      "platform": "linux-glibc-x64",
      "url": "https://publisher.example/sample-tool-1.2.3.tar.gz",
      "checksum": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
      "archiveFormat": "tar.gz",
      "stripComponents": 1,
      "maxDownloadBytes": 50000000,
      "maxExtractedBytes": 200000000,
      "maxFiles": 10000,
      "commands": {"sample-tool": "bin/sample-tool"}
    }]
  }],
  "setupActions": [{
    "id": "install-sample-tool",
    "kind": "managed-tool",
    "tool": "sample-tool",
    "timeoutSeconds": 600
  }]
}
~~~

The zero checksum above is a shape example only; a real manifest must contain the
publisher artifact's verified digest and declare every supported platform explicitly.

Schema-3 Windows command entries must resolve to native `.exe` applications. Batch
files are rejected because running `.cmd` or `.bat` requires a command shell. When a
publisher exposes a script launcher, bind the stable logical command to a verified
native runtime and a verified contained script instead. For example, a Windows Node
artifact can preserve the portable `npm` command without `cmd.exe`:

~~~json
{
  "commands": {
    "npm": {
      "executable": "node.exe",
      "script": "node_modules/npm/bin/npm-cli.js"
    }
  }
}
~~~

Managed artifact paths always use contained, relative, forward-slash syntax on every
host. Schema-1 and schema-2 project manifests remain readable for lifecycle history,
but a schema-2 Windows `.cmd` or `.bat` launcher is intentionally not executable by
the shell-free supervisor. Migrate that entry manually to schema 3 and bind it to the
publisher's trusted native runtime and contained script as above; a generic migrator
cannot safely infer either file or attest that the task is foreground-only.

The report still records the logical command such as `["npm", "ci"]`; the executor
uses the absolute managed application and script paths internally. Unqualified Windows
commands are resolved only from absolute PATH entries, so a same-named executable in
the project working directory cannot shadow a verified managed tool.

`doctor` executes only probes explicitly attested `readOnly: true` and never invokes
setup actions. A schema-3 environment contract must also attest `foregroundOnly: true`
for every process-managed task. The project owner remains responsible for those
attestations.
`setup` is plan-only unless `--apply` is present, computes
the full dependency-ordered action plan before execution, and refuses to run any
action until every declared mutation scope has been approved. Supported scopes are
`network`, `project-files`, `user-files`, and `host-configuration`. Commands are
argument arrays executed without a shell, with bounded output, timeout, exit status,
owned process-group/job cleanup, and command digest evidence. `exec` runs an
ad-hoc project command only after the selected environment passes and injects paths
for verified managed tools. After applying a plan, processctl reruns the original
probes; an installer exit code alone never proves readiness.

The distribution owns detection, planning, bounded execution, HTTPS acquisition,
size limits, checksum verification, safe archive extraction, atomic user-local tool
installation, and exact managed command binding/PATH injection. A consumer owns only
declarative environment
data: exact probes, tool versions and per-platform artifacts, immutable checksums,
project-native dependency commands, dependency edges, and remediation. Project source
does not carry a generic downloader, archive installer, doctor, or setup lifecycle.
Host prerequisites with no safe automated setup action stay blocking.

Probe `readOnly`, foreground-only execution, and command-action mutation scopes are
project-owner attestations, not an operating-system sandbox: `processctl` cannot infer
arbitrary subprocess side effects. Commands must not daemonize, start a detached
session, or leave background work behind. The runner owns a POSIX process group and a
Windows Job Object, but no portable POSIX primitive can contain a deliberately detached
process. Managed-tool actions are stronger—the distribution constrains them to
HTTPS, declared size/checksum/archive/path boundaries and derives their approvals as
`network` plus `user-files`. Use a command action only for project-native package
managers or domain preparation that cannot be represented by the managed-tool
primitive, and declare every possible scope truthfully. New consumers use
project-manifest schema 4. Schema 1 (without an environment contract), schema 2
(the original environment contract), and schema 3 remain readable for backward
compatibility; they are not relabeled as newer shapes. Schema 3 introduced
foreground-only task execution and managed script bindings. Portable impact
declarations and quality extensions are additive optional schema-3 capabilities;
schema 4 adds resource bounds to previously published fields without tightening those
historical readers. New integrations
receive the complete environment contract instead of creating a project-local doctor
or setup lifecycle.

To migrate a live project manifest from schema 3 to 4, keep the same field meanings
and first reduce it to at most 64 profiles, 256 checks per profile, 1,024 checks in
total, and 256 arguments per check, probe, or command setup action; then change
`schemaVersion` and run `processctl contract validate --kind project`. Historical
schema-3 artifacts do not need rewriting. Plan schema 1 follows the same policy:
new plans use schema 2, with at most 256 work/mapping/risk/decision entries and 64
verification profiles per mapping.

## Affected-check selection

Schema 3 and schema 4 optionally declare the same portable impact graph. Components
own canonical forward-slash glob patterns and list downstream components in
`affects`; profile checks list the components that can invalidate them. The
distribution discovers the
committed diff from an exact Git merge base and combines staged, unstaged, and
untracked paths, then computes the transitive component closure and runs only the
selected checks.

~~~json
{
  "impact": {
    "baseRefs": ["origin/main", "main"],
    "unmatchedPaths": "all-scoped-checks",
    "components": [
      {
        "id": "api-contract",
        "paths": ["openapi.json"],
        "affects": ["frontend"]
      },
      {
        "id": "frontend",
        "paths": ["frontend/**"],
        "affects": []
      }
    ]
  },
  "profiles": {
    "development": [
      {
        "id": "frontend-unit",
        "run": ["node", "node_modules/vitest/vitest.mjs", "run"],
        "timeoutSeconds": 900,
        "components": ["frontend"]
      }
    ]
  }
}
~~~

A check without `components` is deliberately always-run. A manifest without an
`impact` object deliberately runs its complete profile through the same runner; this
is suitable for small repositories and is not a legacy execution engine. Any changed
path that matches no component selects every component-scoped check, so an incomplete
graph fails toward broader verification instead of silently omitting evidence.

Standalone verification tries `impact.baseRefs` in order or accepts an explicit
`--base-ref`. Lifecycle verification ignores those defaults and binds selection to
the registered change contract's immutable `comparisonBase`. Inspect a plan without
probing tools or executing checks:

~~~text
processctl verify --project-root . --profile development --plan-only
processctl verify --project-root . --profile development --plan-only \
  --base-ref origin/main --json
~~~

Evidence records the resolved base and merge-base commits, changed and unmatched
paths, direct and transitive components, and a reason for every selected or skipped
check. A selected project command can read that exact immutable scope from the JSON
file named by `ENGINEERING_PROCESS_IMPACT_FILE`. This is intended only for bounded
domain analyzers, such as selecting affected MSBuild projects; changed-path discovery,
component closure, check routing, and evidence remain distribution-owned.

Select capability bundles from `bundles.json`: every consumer starts with `core`,
then adds only capabilities it actually owns. For example, a web product commonly
adds `delivery`, `product`, `api`, `frontend`, and `docs`. Publication from a
completed checkpoint is part of the mandatory core chain. Add
`cross-repo` only when independently versioned repositories participate in one
public-contract change. Re-run `project init ... --replace` with the intended bundle
set when deliberately changing the pin; version remains unchanged during an
unpublished development iteration.

During process development, pass
`--process-root /path/to/engineering-process`; consumer manifests never store that
local path.

`project.json.lifecycle.requiredProfiles` is the minimum evidence for every change.
Individual change contracts may add profiles but cannot remove the baseline.
Every new contract also applies [`production-v1`](PRODUCTION_STANDARD.md) to the ten
portable quality dimensions. Projects may add declared `project-*` dimensions but
cannot remove or weaken the shared minimum. The same contract governs this repository
through its public N-1 self-hosting boundary.
Agents enter non-trivial delivery through the synchronized `run-change` skill; phase
skills are internal owners, not a workflow each project must reconnect.

`process-graph.json` is the machine-readable owner for that chain. It binds every
phase to one owner skill, permitted `processctl` commands, success/failure outcomes,
next phase/skill, and the standing-policy merge boundary. Distribution validation rejects
missing skills, non-core chain owners, nonexistent commands, unknown phases, and
broken handoffs; prose skills explain the graph but do not replace it.

## Canonical lifecycle

Create a change contract from `examples/change.json` and a plan from
`examples/plan.json`. The plan's `contractDigest` is returned by `change start`.

~~~text
processctl change start --contract change.json \
  --actor worker --context worker-session --actor-kind agent

processctl change plan --change-id issue-123 --plan plan.json \
  --actor worker --context worker-session --actor-kind agent

processctl change implement --change-id issue-123 \
  --actor worker --context worker-session --actor-kind agent

processctl change verify --change-id issue-123 --profile development \
  --actor worker --context worker-session --actor-kind agent

processctl change verify --change-id issue-123 --profile review \
  --actor worker --context worker-session --actor-kind agent
~~~

If later remote evidence or a source correction invalidates that verified checkpoint
before review, commit the correction and run `change implement` again. The CLI
preserves the earlier evidence, records `verification-invalidated`, increments the
cycle, and requires every profile again. It accepts this transition only when the
recorded verification is actually stale; a current verified checkpoint cannot use it
to bypass independent review.

After the phase becomes `verified`, a separate reviewer context registers its
assignment:

~~~text
processctl change review start --change-id issue-123 \
  --actor reviewer --context isolated-review-session --actor-kind agent \
  --method isolated-context --attested-by agent-host \
  --attestation-evidence "Host-created isolated read-only context"

processctl change review submit --change-id issue-123 --report review.json
~~~

`changes-requested` returns to `change implement`, which starts a new cycle and
invalidates prior verification and approval. `approved` can advance only while the
source still matches:

~~~text
processctl change finish --change-id issue-123 \
  --actor worker --context worker-session --actor-kind agent
processctl change status --change-id issue-123
~~~

One worker owning specification, planning, implementation, and verification is the
default topology. Bounded helpers are optional optimizations, not required roles;
only review requires a separate actor and context.

Open and deferred findings remain completion-blocking until a later review records
them as resolved or false-positive with evidence. Schema-1 lifecycle state is loaded
through a fail-closed migration that replays immutable review artifacts to reconstruct
pending findings before any transition is allowed.

Completion does not imply commit creation, push, merge, release, or deployment.
Those remain separately gated project workflows. A valid `.process/automation.json`
provides standing authorization, so the host continues each operation automatically
after its owning gate instead of asking for repeated confirmation. An owner directive
may authorize installing that policy but never substitutes for a missing policy.

When new evidence exposes multiple materially valid directions or would change
accepted scope, owner, trust boundary, authority, compatibility, rollout, or
lifecycle order, the coordinator stops dependent mutation and asks the project owner.
It presents evidence, the invariant, real options, trade-offs, and a recommendation;
the accepted decision is recorded before work resumes. Bounded implementation details
already decided by the contract continue autonomously.

The canonical publication order is stricter than a PR-first workflow: implementation
and every required profile pass on a clean checkpoint; a consumer-selected independent
agent or human semantically reviews that checkpoint; findings repeat implementation,
complete verification, and fresh review until approved; `change finish` records
completion; only then may automation push and create the PR. Static policy/secret/pin
checks supplement this review and cannot generate a semantic verdict. With a valid
standing policy, automation then waits for exact-head/current-base required checks and
performs the configured merge without a separate human step.

Completed local evidence can be moved across machines or attached to a release as a
bounded receipt. Export and validate it before any explicit prune:

~~~text
processctl evidence export --project-root . --change-id issue-123 \
  --output issue-123-evidence.json
processctl evidence validate issue-123-evidence.json
processctl evidence prune --project-root . --change-id issue-123 \
  --receipt issue-123-evidence.json
processctl evidence prune --project-root . --change-id issue-123 \
  --receipt issue-123-evidence.json --apply
~~~

The first prune command is a preview. `--apply` is accepted only for a completed run
whose current state matches the validated external receipt. Active, failed,
unexported, mismatched, or tampered evidence remains fail-closed. A partial deletion
failure remains under an explicit `.pruning-*` quarantine and must be recovered from
the retained validated receipt; it is never presented again as a complete local run.

## Publication contract

Validate common metadata before creating or updating a review object:

~~~text
processctl publication validate-branch --branch feat/short-description
processctl publication validate-commit --subject "feat(scope): describe the change"
processctl publication validate-range --project-root . \
  --branch feat/short-description --range origin/main..HEAD
processctl publication validate-pr --title "feat(scope): describe the change" \
  --branch feat/short-description --state draft --body-file pr.md
processctl publication validate-source --project-root . \
  --change-id issue-123 --commit <completed-checkpoint> \
  --title "feat(scope): describe the change" \
  --branch feat/short-description --body-file pr.md
processctl contract validate --kind release release.json
processctl publication validate-release --project-root . \
  --tag v0.2.0 --release-name v0.2.0 \
  --commit <checkpoint> --main-ref origin/main
~~~

Manual branches use `{type}/{kebab-description}`. Automation uses the provider-neutral
`automation/{owner}/{description}` namespace. Commit subjects and PR titles use
Conventional Commit syntax and are limited to 72 characters. Draft PRs may retain
explicitly pending checklist items; every ready PR, including automation, must satisfy
them. The managed template owns the ordered shared sections and immutable standard
checklist meaning. An optional extension after its closing marker uses only
`## Project-specific requirements` plus one-line
`**Project-specific: Label**` checklist items; arbitrary headings, prose, HTML, and
code fences are rejected, and reserved core-policy phrases are rejected anywhere in
an extension item. These checks prevent structural shadowing; independent review
remains responsible for the semantic truth of project-specific evidence.
Raw HTML is outside the supported grammar for both managed `AGENTS.md` contracts and
pull-request descriptions; use visible CommonMark instead.

`validate-pr` remains a metadata-compatibility command. `validate-source` is the
canonical publication gate and requires current completion evidence for the exact
commit, regardless of provider draft/ready presentation.

### Standing gated automation

Projects opt into unattended routine operation with `.process/automation.json`, using
the packaged `automation-policy` schema. The exact policy authorizes commit, push,
review-object publication, merge, release, publication, deployment, adoption, and
ephemeral cleanup only after their existing gates. Merge always requires completed
lifecycle evidence, fresh independent review, exact head, current protected base,
required checks, branch protection, and the configured merge method. Missing or
invalid policy grants no authority.

The confirmation mode is `exceptions-only`. Automation involves the owner only when a
required capability or authority is unavailable, bounded idempotent recovery is
exhausted, or a material product/security decision is missing. Pending checks,
ordinary retries, routine merges, and already authorized external actions continue
without per-action confirmation.

### Controlled automation proposals

Completion-before-publication remains the default. A consumer may enable an untrusted
dependency proposal before completion only through a policy file already present on
the protected base at `.process/automation-proposals.json`. The policy uses the
shape in `examples/automation-proposal-policy.json`: current policy uses schema 2,
selects the target and automation prefix, allows only `dependency-update`,
requires the canonical `lifecycle-completion` check, and fixes every dangerous control
to its fail-closed value. Absence, disablement, a branch-only policy, or a policy digest
mismatch blocks the route.

The immutable provider verifier emits one bounded
`engineering-process-controlled-automation-proposal` report for the exact repository,
base/head, changed paths, title/body digest, owner, controls, and verifier revision.
The adapter resolves `--base-commit` independently from the provider's current target
event; it must not copy that value from the report being checked. Before creating or
updating a proposal, validate the report against the clean source:

~~~text
processctl contract validate --kind automation-proposal-policy \
  .process/automation-proposals.json
processctl contract validate --kind automation-proposal proposal-policy.json
processctl publication validate-proposal --project-root . \
  --policy-evidence proposal-policy.json \
  --repository <owner/repository> --commit <head-sha> \
  --title "chore(deps): update dependencies" \
  --branch automation/renovate/dependencies --target-branch main \
  --base-commit <protected-base-sha> \
  --state draft --body-file pr.md \
  --verifier-repository <owner/verifier> --verifier-commit <verifier-sha>
~~~

This pass proves only that the proposal is safe to expose as untrusted input. It is
not verification, semantic review, completion, or merge authority. Proposal checks
remain read-only and receive no secrets; automerge, scripts, plugins, shell execution,
privileged CI, process-authority, workflow, release, deployment, security-policy, and
trust-root changes are excluded.

The required completion check is absent on every new proposal head. After the exact
head completes the lifecycle, export its receipt, finalize the managed PR requirements,
and have the same immutable verifier produce fresh policy evidence bound to the final
ready body and unchanged base/head. Then run the combined gate:

~~~text
processctl publication validate-proposal-completion --project-root . \
  --policy-evidence proposal-policy.json \
  --evidence completion.json --evidence-kind receipt \
  --repository <owner/repository> --commit <head-sha> \
  --title "chore(deps): update dependencies" \
  --branch automation/renovate/dependencies --target-branch main \
  --base-commit <protected-base-sha> \
  --body-file ready-pr.md \
  --verifier-repository <owner/verifier> --verifier-commit <verifier-sha>
~~~

Only a successful combined gate permits the provider adapter to create
`lifecycle-completion` for that exact SHA. A force update has no inherited check;
branch protection must require the proposal to be current with the exact validated
base; duplicate mismatch fails closed. Historical schema-1 policy remains human-only.
Schema 2 keeps provider automerge disabled before completion and permits merge only
after the protected base's standing automation policy and exact completion gate pass.
Provider tokens, check APIs, branch protection, retries, and repository selection
remain consumer-owned adapter behavior.

## Trust boundary

The CLI proves structural separation: reviewer actor id and context id must both be
unused by implementation, every review assignment in the project must use a fresh
context id, and the review must match the verified checkpoint. The agent host or
human organization owns the truth of the identity attestation. A host adapter should
create a read-only isolated context with no inherited implementation or prior-review
conversation, pass stable identities to `change review start`, and preserve its
evidence. A stable reviewer actor or role may be reused with a fresh context; merely
renaming retained context does not satisfy the process.

Self-hosted verifier, signing, release-controller, and process-authority changes use
the portable authority-rotation rule: the old trust root governs introduction, the
new root is published under an immutable identity before consumers pin it, cutover is
proved without a control gap, and retirement happens only after the new boundary is
active. Provider-specific mechanics may require multiple independently completed
changes; normal product changes do not inherit that staging automatically.

`change review submit` may be invoked by a coordinator transporting the assigned
reviewer's exact report. The CLI validates that artifact against the assignment and
carried findings; the attesting host or human boundary, not local process state,
authenticates who produced it.

The producer release workflows implement the same host-neutral chain with explicit
artifacts and callbacks: `release-pr.yml` creates only an unpublished Git bundle;
`release-candidate.yml` restores it and runs `change start`, `change plan`,
`change implement`, and every required `change verify`; the resulting
`engineering-process-review-required` event names the exact artifact and checkpoint.
The consumer-selected host restores that lifecycle, chooses an agent or human,
registers the assignment, submits the exact report, resolves any finding loop, runs
`change finish`, and exports completion evidence. It sends only that bounded
gzip/base64 evidence to `release-approval.yml`; semantic reports and reviewer selection
never become workflow inputs. The publication workflow validates the receipt against
the exact clean source with `publication validate-evidence-source`, and only then
pushes the branch and creates a ready PR. No workflow invokes merge.

The host callback is deterministic after semantic completion:

~~~text
processctl evidence export --change-id <change-id> --output completion.json
processctl evidence encode-completion --evidence completion.json \
  --evidence-kind receipt --output completion.txt
gh workflow run release-approval.yml --ref main \
  -f verified_run_id=<verified-run-id> \
  -f comparison_base=<base-sha> \
  -f release_head_sha=<completed-checkpoint> \
  -f completion_evidence_gzip_base64="$(<completion.txt)"
~~~

A bootstrap-authority release uses `evidence export-bootstrap` and
`--evidence-kind bootstrap-authorization`. The adapter rejects oversized or malformed
transport, a different process identity, base, project, checkpoint, workspace
fingerprint, source tree, or publication range.

## Distribution contracts

- `project.json` declares baseline profiles and exact argument-array checks.
- `process.lock` pins the process version, selected skills, and a digest covering the
  runtime, canonical exact runtime/build/development dependency locks, schemas,
  templates, bundle catalog, and
  complete selected skill resources. Startup fails when installed runtime dependency
  versions differ from that lock.
- `requirements/process.in` owns the direct authority pin. Renovate uses the
  pip-compile manager to update its complete binary-only hash lock, then the managed
  `.process/adopt-process.py` runner rejects symlink, junction, or reparse input in
  every supplied path component, snapshots one bounded stable copy outside the
  checkout, binds every path component against concurrent retargeting, and uses that
  exact digest for installation and `processctl adoption apply`. POSIX process groups
  and a managed Windows kill-on-close Job Object contain every child. The resulting
  draft contains the new lock, managed contracts, skill snapshots, and any
  target-version consumer-owned project migration; after CI and fresh-context
  independent review, merge is the end of adoption.
- The repository-root GitHub Action is the shared CI bootstrap surface. Consumers pin
  its full governed release commit, while the exact Python authority remains selected
  exclusively by their hash-locked `requirements/process.txt`. The action invokes the
  producer-owned installer from its immutable action checkout and never downloads or
  executes helper source from the consumer branch.
- Versioned JSON schemas define change, plan, verification, review, lifecycle,
  completion-related artifacts, release-change fragments, and the release
  classification contract. The generated Release PR gate binds that contract to the
  exact SemVer increment, package version, latest reachable prior tag, reviewed head,
  identical merge tree, immutable checkpoint, and main ancestry.
- Remote matrix jobs publish one bounded supplemental-verification schema-2 bundle
  per platform/runtime. Its manifest binds the exact source and workflow checkpoints,
  automation actor/context, run URL, platform/runtime identity, selected impact,
  configured timeouts, output byte counts/digests, redacted diagnostic summaries,
  truncation state, and the hashes of its schema-3 profile reports. Historical
  supplemental schema-1 bundles and verification schema-1/schema-2 reports remain
  readable under their released semantics. GitHub's artifact id and digest complete
  the immutable remote reference; this supplements rather than replaces N-1
  lifecycle evidence.
- New lifecycle work uses bounded plan schema 2. Selective-impact consumers may add
  the optional capability on project schema 3, while new integrations use bounded
  project schema 4. Plan schema 1 and the pre-existing fields of project schemas 1-3
  retain their published validation behavior instead of being tightened in place.
- `release.json` is the single release-identity owner. Governed GitHub tag and title
  are both exactly `v<SemVer>`; package metadata, runtime version, artifact names,
  authorization evidence, and later consumer locks must match it. Public-impact PRs
  add bounded `release-changes/<id>.json` fragments; automation aggregates them into
  one reviewed Release PR and never writes a chosen version directly to protected
  `main`. Recorded bootstrap history transitions once through a separately typed
  bootstrap-authority bundle, then all later releases require a public N-1 lifecycle
  receipt.
- `VERSIONING.md` owns package-versus-schema classification and the explicit
  Renovate-assisted adoption boundary. `processctl publication prepare-release`
  derives and materializes the only permitted next package version from the complete
  fragment set.
- Project commands run without a shell and inherit the caller environment. Never put
  secrets in manifests, arguments, or reports.
- Consumer skill roots are distribution-owned: unmanaged `SKILL.md` files or catalog
  files fail `sync` and `doctor`. Project-specific policy belongs in `AGENTS.md`,
  product contracts, source, and the manifest's command bindings.
- Host-specific launchers, agent role files, and model settings are optional external
  integrations. They are neither bundled into the core nor required in consumer
  repositories.
- The managed pull-request template and publication validators are shared process
  policy. Consumer repositories may append project-specific requirements after the
  managed block but do not copy or redefine the common convention.

## Development

~~~text
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python verification/run_test_suite.py
.venv/bin/python processctl.py skills validate --root process_assets/skills
.venv/bin/python processctl.py digest
~~~

Version 0.x remains a compatibility pilot. A 1.0 release requires publishing the CLI,
running consumer CI through the published artifact, and completing forward tests on
representative agent hosts. Portable evaluation fixtures live in `evals/cases.json`.
Automated Release PR authorization, repository controls, recovery rules, and the
secretless PyPI publisher identity are defined in
[`RELEASING.md`](https://github.com/phuongnse/engineering-process/blob/main/RELEASING.md).
