# Consumer setup

This page is for a repository adopting Engineering Process. It answers only the
first setup questions; [SELF_HOSTING.md](../SELF_HOSTING.md) is the detailed
adoption reference, and [ARTIFACT_STANDARDS.md](../ARTIFACT_STANDARDS.md) covers
document formats.

## Install the process

Pin the package and its hash in the consumer's requirements lock. Then apply the
same installed package to the checkout:

    processctl adoption apply \
      --project-root . \
      --requirements-lock requirements/process.txt

Adoption writes only managed surfaces: portable skills, the marked instructions
block, the managed PR-template block, the adopter, the process lock, and the
current process configuration. It preserves consumer-owned skills, instructions,
standards, product documents, and tool configuration. Run the adoption check after
installing and whenever the package or selected standard changes.

The current package has one current definition for each process-owned contract.
When a release changes that definition, the consumer updates the pin and hash,
recreates unsupported active artifacts, regenerates managed output, and reruns its
required profiles. It does not ask an older process release to interpret the new
contract.

## Minimal project configuration

The consumer owns .process/project.json. Keep commands as arrays with finite
timeouts:

~~~json
{
  "schemaVersion": 1,
  "project": "my-project",
  "lifecycle": {
    "requiredProfiles": ["development", "review"]
  },
  "profiles": {
    "development": [
      {
        "id": "tests",
        "run": ["python", "-m", "unittest"],
        "timeoutSeconds": 600
      }
    ],
    "review": [
      {
        "id": "package",
        "run": ["python", "-m", "build"],
        "timeoutSeconds": 600
      }
    ]
  }
}
~~~

The consumer chooses the commands, setup actions, profile names, branch policy,
and document structure. The bounded runner limits execution and stores bounded
metadata; it does not install dependencies or decide whether a command proves a
product capability.

Validate the project before starting delivery:

    processctl project validate --json
    processctl doctor
    processctl adoption check --requirements-lock requirements/process.txt

Use the repository's supported Python interpreter to invoke the installed
processctl. A consumer that uses readiness declares its target, stage, immutable
pack versions, and capability evidence in .process/readiness.json. An enforced
capability must map to real required profiles; a planned gap remains visible but
does not become an unrelated change objective. The full readiness rules live in
the consumer's current process configuration and the ownership map in
[README](../README.md#ownership-and-source-boundaries).

## Start work

Read [docs/delivery.md](delivery.md), then enter through the managed
deliver-change skill. Do not select a later phase from memory or edit
implementation before the lifecycle says the change is specified or planned as
appropriate.

For every change, the consumer decides whether users, operators, developers, or
future maintainers need new knowledge. If they do, update the consumer-owned
source during the same change. If they do not, preserve the reasoning in the plan
or change description; do not create a blank page or a documentation checkbox.
See [Documentation quality](documentation.md).

## Ownership after adoption

The process does not own the consumer's product docs, API reference, runbooks,
examples, or release policy. The consumer decides where those live and how they
are rendered. The process supplies:

- lifecycle guidance and evidence boundaries;
- optional current-v1 artifact standards and adapters;
- managed skills and adoption scripts;
- rules for finding and reviewing documentation impact.

The consumer keeps its existing documentation organization unless a concrete
reader problem calls for a focused repair. A new consumer needs only the smallest
useful entry point; adoption does not generate a documentation tree full of empty
pages.
