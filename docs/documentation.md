# Documentation quality

This is the process rule for caring for consumer documentation during ordinary
delivery work. It does not impose a universal folder tree, document inventory,
format, or writing style. The consumer owns its product knowledge and keeps its
existing organization unless a concrete reader problem requires a focused repair.

## The reader's job comes first

A useful document lets the intended reader find the needed fact, understand it,
and act without reconstructing the implementation conversation. Identify the
reader and task before choosing a file:

| Reader | Typical question |
| --- | --- |
| User | How do I install, configure, or use this behavior? |
| Operator | How do I run, monitor, recover, or troubleshoot it? |
| Developer | Where is the API, contract, example, or design decision? |
| Maintainer | What is authoritative, generated, adopted, or historical? |

The question determines the useful source. A user-facing behavior may need a
guide or example; an operational change may need a runbook; an internal refactor
may need no document change. “Documentation updated” is not an outcome by itself.

## A small decision flow

1. **Locate the entry point.** Find where the consumer's intended reader already
   starts. Read the nearby guide and reference before creating a new page.
2. **Name the changed knowledge.** Describe the behavior, configuration, limit,
   operational action, API, or decision that becomes different.
3. **Choose the smallest useful source.** Update an existing authoritative page,
   add a focused page only when no current source can hold the information, or
   record a reasoned no-impact decision in the change plan.
4. **Keep one authority.** A summary may point to the source, but two documents
   must not independently define the same rule. Mark retained historical material
   as historical and repair links and entry points with it.
5. **Update with implementation.** Examples, configuration snippets, generated
   output, and cross-references are part of the same change when they are the
   reader's actual way to act.
6. **Try the reader path.** Start from the documented entry point and perform a
   representative task without adding context from the implementation chat.

This is a decision, not a mandatory artifact. A change with no reader impact does
not need a new page or an empty “not applicable” section.

## Source and derived output

When a document is generated, the source definition and generator are authoritative.
Fix the source or generation mechanism, regenerate the output, and inspect the
actual bytes or rendered result. Do not hand-repair one generated copy.

For this distribution:

| Surface | Source of truth | Derived or adopted boundary |
| --- | --- | --- |
| Lifecycle guidance | process_assets/skills | .agents/skills after consumer adoption |
| PR template guidance | process_assets/standards/pull-request.v1.json | templates/PULL_REQUEST_TEMPLATE.md, Renovate preset, then adopted managed block |
| Process instructions | templates/AGENTS.process.md | The marked block in a consumer's AGENTS.md |
| Runtime behavior and data shape | engineering_process and schemas | Packaged distribution and consumer lock |
| Release description | release.json and release fragments | RELEASE_NOTES.md at release preparation |

The current self-consumer is pinned to the released package. A source change for
the next distribution can therefore differ from its currently adopted managed
files; the release and adoption instructions must say what the consumer will
regenerate and when. This prevents a documentation fix from creating adoption
drift.

## How the lifecycle checks documentation

At start and plan, connect a documentation decision to an accepted outcome and
literal work-item paths. During implementation, update consumer-owned source and
regenerate outputs. During verification, use automated checks for properties they
can actually observe: links resolve, examples run, structured source and output
agree, or bytes match. During review, judge whether the text is accurate,
understandable, findable, and sufficient for the representative task.

Do not infer usefulness from page count, heading count, line count, file names,
keyword presence, or a passing formatter. Do not turn documentation care into a
second lifecycle or a universal approval gate. A review observation should name
the wrong, missing, misleading, hard-to-find, or hard-to-use information and the
consequence for a supported reader.

## Practical examples

- A new CLI option needs its usage, constraints, and an example updated at the
  consumer's command reference; an internal test rename usually does not.
- A new configuration field needs the setup path, default/required behavior, and
  any generated config example that readers actually copy.
- A changed recovery boundary needs the operator action and failure distinction,
  not a pasted implementation log.
- A design decision that future developers must preserve belongs near the
  consumer-owned code or design reference, not only in a review report.
- A process skill correction updates the source skill and its package/adoption
  instructions; it does not require every consumer to reorganize its product docs.

## Review questions

- Can a new reader start from the documented entry point without this conversation?
- Can a person doing the current work find the phase instruction and the authoritative
  source without following a maze of links?
- Does the document explain what changed, who needs to act, and any meaningful
  limits or recovery path?
- Is the source current, or is it clearly marked as generated, adopted, or history?
- Did the change update the actual source of a generated result and verify the
  resulting output?

The process reviews these properties while the consumer reviews product truth.
