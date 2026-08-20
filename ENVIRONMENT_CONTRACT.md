# Environment and probe contract

This document owns the public environment-readiness and finite-command evidence
semantics referenced by [`PRODUCTION_STANDARD.md`](PRODUCTION_STANDARD.md). Projects
declare requirements, probes, remediation, managed artifacts, setup actions, and
verification commands; the process owns bounded execution and evidence shape.

## Requirement probes

A probe is a finite, non-interactive, project-attested read-only command. Its declared
output selector chooses standard output, standard error, or their combined view. An
optional regular expression evaluates a bounded matching view under the probe's
remaining time budget.

For portable matching only, CRLF and standalone CR line boundaries are canonicalized
to LF. This normalization does not alter captured evidence. The report retains the
original output bytes, byte counts, truncation state, and digests, so environments may
agree on readiness without claiming different source evidence was identical.

Output matching is bounded independently of command execution. A timeout, unsafe
expression cost, unavailable stream, failed command, or unmatched expression leaves
the requirement unsatisfied with an attributable reason. An installer exit code never
substitutes for a successful post-setup probe.

## Finite-task boundary

Commands are argument arrays executed without a shell. The process owns non-interactive
input, working-directory containment, managed command binding, output limits, timeout,
exit status, and descendant cleanup. Project attestations such as read-only behavior
or mutation scope are policy claims, not an operating-system sandbox.

Services, interactive protocols, watchers, detached processes, and log followers are
outside this finite-task contract. A project keeps them in its own service lifecycle
until a separate portable contract owns their readiness and cleanup semantics.

## Evidence ownership

Environment reports preserve the declared requirement and operation identity,
timestamps, result, original output metadata, and bounded diagnostic reason. Secrets
do not belong in probe arguments or reports. Platform-specific process containment,
path resolution, and executable launching remain implementation details behind this
contract and must not change its observable evidence meaning.
