# Consumer artifact standards

The process supplies versioned defaults. Consumers select the document requirements;
generation and verification resolve the same definition. Document checks do not grant
lifecycle approval, merge permission or release authority.

| Default | Adapter | Input and verification |
| --- | --- | --- |
| `pull-request@1` | `pr-description` | Ordered sections, fields and checkboxes; structural validation, or exact rendering from supplied data |
| `release-notes@1` | `release-notes` | Consumer change/source records and upgrade text; exact rendered-byte comparison |
| `automation-name@1` | `automation-name` | Ordered owner/role components by default; exact name comparison |
| `issue@1` | `issue` | Open request and closed resolution records; exact rendered-byte comparison |

The selected package supplies these immutable defaults. A future standard version
gets another file and explicit selection; an existing version is not edited after
publication. There is no remote lookup, code plugin or additional lifecycle.

## Select or override

Without `.process/standards.json`, each artifact uses its packaged `@1` default.
Inspect or export a definition:

    processctl artifact show --artifact pull-request --json
    processctl artifact show --artifact pull-request --output .process/company-pr.json

Give the exported definition a consumer-owned `id` and `version`, then edit its
headings, ordered fields/checklists or other supported rules. Keep stable field IDs
when changing labels; generated data refers to IDs. Each declared field is required.
Add a field to require more information, or remove it when the consumer does not want
it in this document. Lifecycle verification and independent review still apply.

Select it in `.process/standards.json`:

```json
{
  "schemaVersion": 1,
  "artifacts": {
    "pull-request": {"path": ".process/company-pr.json"},
    "release-notes": {"builtin": "release-notes@1"}
  }
}
```

An override is a complete definition, with no implicit deep merge. Its shape follows
the packaged `artifact-standard` schema. Invalid definitions, missing paths,
unsupported adapters/versions and artifact mismatches fail; explicit selection never
silently falls back to a default. Paths must be canonical files within the consumer
Git snapshot, outside ignored directories and lifecycle state. Links are rejected.
Commit the selection and definitions with the consumer change.
Adoption rejects a selection that occupies a file it must replace or retire, before
writing or deleting anything. Move such definitions to consumer-owned paths and update
the selection. This ownership check covers every selected artifact, including release
definitions when adoption only needs to render a PR template.

Adoption preserves those files and renders the managed PR-template block from the
selected PR standard. Do not hand-edit that generated block. Changing the definition
requires regenerating the template and collecting fresh verification evidence.

## Issues

The default issue record covers the accepted request while open and adds resolution,
implementation, verification, release/adoption/consumer confirmation, remaining risk,
and follow-up evidence when closed. The record is portable Markdown: its first line is
the title and the remaining sections are the body. Tracker creation, updates, closure,
labels, priority, assignment, and product taxonomy remain consumer-owned actions.

Supply values under stable field IDs and render the selected state:

```json
{
  "schemaVersion": 1,
  "title": "Preserve bounded issue evidence",
  "fields": {
    "context": "Consumer issues are durable lifecycle sources.",
    "expected-outcome": "Use one selected issue standard.",
    "evidence": "The current consumer template varies by author.",
    "scope": "Issue records only; no tracker API.",
    "acceptance-criteria": "The generated open record validates exactly.",
    "references": "https://example.com/issues/12"
  },
  "checks": {}
}
```

    processctl artifact render --artifact issue --state open --data-file issue-data.json --output issue.md
    processctl artifact validate --artifact issue --state open --data-file issue-data.json --body-file issue.md
    processctl artifact render --artifact issue --state closed --data-file closure-data.json --output closure.md
    processctl artifact validate --artifact issue --state closed --data-file closure-data.json --body-file closure.md

Open and closed records reject missing or reserved pending values. Closed data includes
the open fields plus every selected closure field. `record-url` requires one HTTPS URL;
`record-references` accepts `none` or comma-separated HTTPS URLs. These are declared
protocol checks, not judgments about prose truth or whether an external tracker changed.

References reject raw control characters and whitespace, credentials, malformed URI
authorities, and invalid or out-of-range ports. Registered names use the RFC 3986
authority grammar; IPv6 literals are supported. Use IDNA or percent-encoding for
international host names. Paths, queries, and fragments remain part of the reference.
A configured title prefix cannot replace the required nonblank title value.

Consumers can override title prefix/length, headings, field order and labels, add or
remove fields/checks, and select reference formats. Keep stable IDs used by consumer
data. A process-improvement repository can therefore select a stricter stable title
prefix without imposing it on product issues.

## PR descriptions and Renovate

Generate the selected template or a matching consumer-owned Renovate preset:

    processctl artifact template --artifact pull-request --output PR_TEMPLATE.md
    processctl artifact renovate-preset --artifact pull-request --output .github/renovate-pr-body.json

Configure Renovate to use the generated `prHeader` and `prBodyTemplate` through its
consumer-owned configuration. Re-run that command when the definition changes and
check the generated file in a consumer profile. Bootstrap the matching configuration
on the protected base before relying on new bot drafts: Renovate selects its body
configuration before the dependency update and adoption task run.

The bot can supply the update and package-file facts for the stable `outcome` and
`scope` IDs. The author/coordinator inspects the full adoption scope and fills known
contract facts, then actual verification, independent-review and completion results.
The reviewer owns the verdict; the user need not fill all placeholders manually.

For a draft, save known values under stable IDs:

```json
{
  "schemaVersion": 1,
  "fields": {
    "outcome": "Adopt the selected public process version.",
    "scope": "Package lock and generated adoption files."
  },
  "checks": {}
}
```

    processctl artifact render --artifact pull-request --state draft --data-file pr-data.json --output pr-body.md
    processctl artifact validate --artifact pull-request --state draft --body-file pr-body.md
    processctl artifact validate --artifact pull-request --state ready --body-file pr-body.md

The default reserves `pending`, `pending...` and `pending…`, compared as whole values
after trimming whitespace and ignoring case. Drafts may contain these values and
unchecked items. Ready documents require resolved values and checked items. This is a
closed placeholder protocol, not a guess about arbitrary prose; technical text that
mentions pending work is not automatically classified as unfinished evidence.

Render ready data only after filling the remaining fields/checks from real results.
The flag does not invent those results. Optional `issueReference` follows the selected
issue-reference rule. Default drafts cannot close issues, and final consumer issue
closure still requires the ordinary completion guidance.

`publication validate-pr` retains its title/branch checks and now uses the selected
PR body standard. `--project-root` selects the consumer explicitly; existing calls
default to the current directory. For data-generated descriptions, add `--data-file`
to `artifact validate` to require exact UTF-8/LF output from those records as well.

## Release notes

The consumer maps its release records to the packaged `release-notes-data` schema.
This input belongs to the consumer; it does not replace its versioning policy or
become another authoritative changelog. For example:

```json
{
  "schemaVersion": 1,
  "title": "Example v1.2.1",
  "introduction": "Changes since v1.2.0.",
  "changes": [
    {"type": "fix", "summary": "Preserve the requested behavior.", "source": "CHANGE-12"}
  ],
  "sections": {"upgrade": "No migration required."}
}
```

    processctl artifact render --artifact release-notes --data-file release-data.json --output RELEASE_NOTES.md
    processctl artifact validate --artifact release-notes --data-file release-data.json --body-file RELEASE_NOTES.md

Both operations default to ready. Validation requires the source data; it does not
accept headings alone as proof of release contents. Every change type must have a
selected group, and the data must supply exactly the selected sections. A consumer
override can reorder/rename groups or require additional sections such as upgrade or
security impact. Update the consumer's data mapping when changing IDs or requirements.

Change summaries are literal text. HTTP(S) source references become links; other
owned references remain code spans without invented GitHub links. `repositoryUrl`
can identify the consumer's GitHub repository for compact issue/PR link labels.
The title, introduction, section contents and optional footer are consumer-authored
Markdown. Export is UTF-8 without BOM using LF. `--data-file` validation compares
the exact output bytes, including the final newline.
Artifact bodies are bounded to 1 MB; JSON inputs use the existing 2 MB process limit.

This producer's existing release wrapper maps `release.json` and its upgrade guidance
to that input. Published release bodies remain immutable.

## Automation names

The default convention is a lowercase owner identifier followed by a clear automation
role, separated by a hyphen. Consumers supply the actual owner and role; the process
does not embed a particular account name or judge a role's meaning from keywords.

```json
{
  "schemaVersion": 1,
  "components": {"owner": "Acme", "role": "Dependency-Updates"}
}
```

    processctl artifact render --artifact automation-name --data-file automation-name.json --json
    processctl artifact render --artifact automation-name --data-file automation-name.json --output automation-name.txt
    processctl artifact validate --artifact automation-name --data-file automation-name.json --body-file automation-name.txt

Render without `--output` returns the UTF-8/LF value in the JSON result's `content`
field, alongside the same standard/data/artifact digests. A consumer bootstrap can
compare the actual provider field plus LF to this content before side effects.
Do not reconstruct the pattern in another validator. Recheck the provider-returned
name before storing credentials or applying configuration, since registration UIs
may allow the proposed name to be edited.

An `automation-name` override can choose ordered component IDs, a `-`, `_` or `.`
separator, `lower` or `preserve` case, and a maximum length. Components are bounded
alphanumeric words with optional internal hyphens; every declared component must be
provided and extra components fail. Naming requires complete data in both draft and
ready states, so this adapter has no placeholder vocabulary. Entirely different name
formats remain consumer-owned validators.

The consumer also enforces provider constraints and existing identity bindings.
Naming conformance does not establish ownership, permission, uniqueness, or trust.
Keep an already conforming name unless a rename is explicitly accepted; a rename may
require migration of provider slugs, event sender checks and credential bindings.
For example, renovate-ops owns GitHub-specific checks and where the shared naming
result is applied during App bootstrap; this distribution owns the default convention.

## Verification and extension boundary

Artifact results name the effective standard ID, version, canonical digest and source,
plus the artifact digest and, when provided, the input-data digest. Put the applicable
command in an existing required consumer profile. Ordinary captured command evidence
and repository snapshots remain the evidence chain. Editing a selected override
invalidates the previous snapshot; adopting a package also changes the selected
distribution. External PR metadata still needs current consumer CI checks.

The schemas describe the supported formats. Entirely custom layouts or semantic
checks use consumer-owned template, renderer and validator commands in existing
profiles. Select an independent consumer template or native bot body configuration;
the managed default template can coexist with those files. Replace the default body
check with the custom validator while preserving the consumer's other required
checks. An unsupported format is never reported as verified by a built-in adapter.

The selection envelope accepts other artifact IDs. A document using the existing PR
field/checklist shape can reuse that adapter with its own definition. A new format
requires a separately implemented, schema-validated adapter; it reuses selection and
ordinary profiles without adding lifecycle phases or evidence stores.
