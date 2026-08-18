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
- Make schema changes backward compatible within a major version or provide a
  migration and major-version change.
- Add regression coverage for every deterministic process defect.

## Verification

Run:

~~~text
python -m unittest discover -s tests -p test_*.py
python processctl.py skills validate --root .agents/skills
~~~
