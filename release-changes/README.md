# Release changes

Add one JSON file per externally meaningful change. If one pull request resolves
several issues, use one fragment per issue or independently adoptable behavior; do
not replace those records with an issue range:

    {
      "schemaVersion": 1,
      "id": "short-change-id",
      "type": "fix",
      "summary": "Short user-facing change title.",
      "source": "https://github.com/example/project/issues/123",
      "details": {
        "problem": "What problem or limitation existed.",
        "changes": "What behavior was added, fixed, or changed.",
        "affectedPaths": ["src/owner.py", "tests/test_owner.py"],
        "apply": "What the consumer must run, configure, or review.",
        "compatibility": "Breaking or non-breaking impact and consumer action.",
        "notes": "Important limits, caveats, or retained behavior."
      }
    }

`source` identifies exactly one issue, pull request, or owned change. Do not put an
issue range, several issue URLs, or a pull-request summary that hides independently
adoptable behavior in one fragment. The six structured detail fields remain required
in the manifest so reviewers can inspect the full record, while the selected release
standard projects only the detail useful for that change type. `compatibility` must
explicitly state whether the item is breaking; use `type: "breaking"` when it is.

The renderer treats the summary and detail values as literal single-line metadata:
ordinary punctuation remains readable, while Markdown and HTML-sensitive syntax is
escaped. Source URLs become links and owned references become inline code. Do not
pre-escape values in JSON or hand-edit the generated release body.

Allowed types are fix, capability, and breaking. The current definition requires
`schemaVersion: 1` and complete detail fields; non-current fragments are rejected
before writing any release files. Historical release fragments remain only in the
commits that published them and are not read by the current preparation command.
