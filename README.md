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

Python 3.11 or newer and Git are required. Lifecycle state is stored under ignored
`.process/runs/`; completion, review, and verification are bound to a clean Git
checkpoint and workspace fingerprint.

## Consumer bootstrap

Add only project-owned configuration:

~~~text
project/
├── AGENTS.md
├── .github/
│   └── PULL_REQUEST_TEMPLATE.md
├── .gitignore             # includes .process/runs/
└── .process/
    └── project.json       # project profiles and lifecycle baseline
~~~

Install `processctl` from a tagged release and create a candidate manifest from
`examples/project.json` with the repository's real commands. Bootstrap the complete
standard in one command:

~~~text
processctl project init --project-root . --manifest project.json \
  --bundle core --bundle delivery --bundle product
processctl doctor --project-root .
~~~

`project init` validates the manifest, preflights ownership conflicts, writes the
lock, installs the managed `AGENTS.md` and pull-request contracts, adds the ignored
lifecycle-state path, and synchronizes the selected skills. It refuses to replace
differing project configuration or unmanaged skills unless the conflict is resolved
explicitly. `sync --check` and `doctor` detect drift in skills, the managed agent
contract, and the pull-request block. A consumer never authors or maintains process
skills locally.

Select capability bundles from `bundles.json`: every consumer starts with `core`,
then adds only capabilities it actually owns. For example, a web product commonly
adds `delivery`, `product`, `api`, `frontend`, `docs`, and `publication`. Add
`cross-repo` only when independently versioned repositories participate in one
public-contract change. Re-run `project init ... --replace` with the intended bundle
set when deliberately changing the pin; version remains unchanged during an
unpublished development iteration.

During process development, pass
`--process-root /path/to/engineering-process`; consumer manifests never store that
local path.

`project.json.lifecycle.requiredProfiles` is the minimum evidence for every change.
Individual change contracts may add profiles but cannot remove the baseline.
Agents enter non-trivial delivery through the synchronized `run-change` skill; phase
skills are internal owners, not a workflow each project must reconnect.

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
Those remain separately authorized project workflows.

## Publication contract

Validate common metadata before creating or updating a review object:

~~~text
processctl publication validate-branch --branch feat/short-description
processctl publication validate-commit --subject "feat(scope): describe the change"
processctl publication validate-range --project-root . \
  --branch feat/short-description --range origin/main..HEAD
processctl publication validate-pr --title "feat(scope): describe the change" \
  --branch feat/short-description --state draft --body-file pr.md
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

## Trust boundary

The CLI proves structural separation: reviewer actor id and context id must both be
unused by implementation, and the review must match the verified checkpoint. The
agent host or human organization owns the truth of the identity attestation. A host
adapter should create a read-only isolated context, pass stable identities to
`change review start`, and preserve its evidence. Self-asserted separation without a
host or human attestation does not satisfy the process.

`change review submit` may be invoked by a coordinator transporting the assigned
reviewer's exact report. The CLI validates that artifact against the assignment and
carried findings; the attesting host or human boundary, not local process state,
authenticates who produced it.

## Distribution contracts

- `project.json` declares baseline profiles and exact argument-array checks.
- `process.lock` pins the process version, selected skills, and a digest covering the
  runtime, schemas, templates, bundle catalog, and complete selected skill resources.
- Versioned JSON schemas define change, plan, verification, review, lifecycle, and
  completion-related artifacts.
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
python -m unittest discover -s tests -p 'test_*.py'
python processctl.py skills validate --root .agents/skills
python processctl.py digest
~~~

Version 0.x remains a compatibility pilot. A 1.0 release requires publishing the CLI,
running consumer CI through the published artifact, and completing forward tests on
representative agent hosts. Portable evaluation fixtures live in `evals/cases.json`.
