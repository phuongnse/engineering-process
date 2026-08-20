# Engineering Process Agent Contract

This repository owns an agent-neutral engineering process. Keep normative behavior
portable across skills-compatible agents and keep deterministic enforcement in the
CLI, schemas, and tests.

## Rules

- Do not name or require a model, agent product, subagent API, proprietary tool, or
  vendor-specific instruction in core skills.
- Keep skill frontmatter compatible with the Agent Skills specification.
- Treat processctl JSON contracts and exit codes as the enforcement boundary.
- Never execute project commands through a shell. Commands are argument arrays.
- Do not place secrets in manifests, command arguments, or evidence reports.
- Preserve ownership: this repository owns the complete lifecycle and generic gates;
  consumer repositories own domain policy, commands, durable product specifications,
  acceptance content, and release decisions.
- Govern this repository through the public N-1 distribution pinned in
  `.process/process.lock`; source under development is a verification target, never
  its own lifecycle authority. Keep managed N-1 skills in `.agents/skills` and edit
  only N+1 distribution sources under `process_assets/skills`.
- Treat [`VERSIONING.md`](VERSIONING.md) as the normative package, schema, release,
  and adoption version policy. Treat the CLI, exit codes, schemas, lifecycle
  transitions, evidence shapes, managed assets, and documented semantics as the
  public API. Derive every release increment from that surface before changing its
  version.
- Treat integer `schemaVersion` values as compatibility majors for serialized data.
  Additive optional fields do not increment them. Do not alter a released schema's
  meaning or remove its reader without an explicit deprecation, migration, and
  package-major transition.
- Make schema changes backward compatible within a major version or provide a
  migration and major-version change.
- Add regression coverage for every deterministic process defect.
- Apply `PRODUCTION_STANDARD.md` to this repository and every portable consumer:
  assess all core quality dimensions, bind applicable dimensions to measurable
  criteria/evidence, and let projects add but never weaken `project-*` extensions.
- Bound repository-controlled and remote work by time, count, item size, aggregate
  size, output, and descendant lifetime. Keep lifecycle evidence durable; clean
  ephemeral/build state deterministically on success, failure, timeout, and interrupt.
- Derive package, runtime, tag, GitHub release title, artifact, receipt, and consumer
  lock identity from the release contract; never update one surface independently.

## Verification

Run:

~~~text
python -m unittest discover -s tests -p test_*.py
python processctl.py skills validate --root process_assets/skills
~~~

<!-- engineering-process:start -->
## Engineering process

Use the portable skills pinned by `.process/process.lock` for every non-trivial
change. Enter through `run-change` and use `processctl change ...` for specification,
planning, implementation registration, checkpoint verification, independent review,
finding resolution, and completion.

The project owns product decisions, domain contracts, exact verification commands,
and publication authority. The process distribution owns lifecycle semantics and
managed skills. Do not edit managed skills in this repository; update the pinned
distribution and synchronize them instead.

Independent review requires an attested read-only actor and context that did not
implement the current cycle. No particular agent host is required. Missing or stale
evidence, self-review, and publication without separate authorization are blocking.
<!-- engineering-process:end -->
