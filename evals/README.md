# Cross-agent evaluation

Run every case in cases.json against each supported agent host with only the named
skill, prompt, and a minimal repository fixture. Score mustInclude and mustNotInclude
items from the emitted artifact and trace. Do not provide the expected answer to the
agent.

A release candidate passes portability only when every required host satisfies every
hard assertion. Compare task fidelity, false activation, tool calls, elapsed time, and
user interruptions. Different reasoning paths are acceptable; contract outputs and
gate outcomes must remain equivalent.
