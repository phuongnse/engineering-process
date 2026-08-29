# Release changes

Add one JSON file per public change:

    {
      "schemaVersion": 1,
      "id": "short-change-id",
      "type": "fix",
      "summary": "Observable release note.",
      "source": "issue-or-change-reference"
    }

Allowed types are fix, capability, and breaking. The Prepare release PR workflow
validates and consumes all fragments.
