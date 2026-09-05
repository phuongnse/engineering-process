#!/usr/bin/env python3
"""Generate the optional Renovate preset from the canonical public template."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engineering_process.contracts import ProcessError  # noqa: E402
from engineering_process.publication_compat import (  # noqa: E402
    _pull_request_body_issues,
    _without_html_comments,
)


def generate_preset(template: str) -> dict[str, str]:
    body, issues = _without_html_comments(template)
    if issues:
        raise ProcessError("; ".join(issues))
    body = re.sub(r"(?m)^(- [^:\n]+:)[ \t]*$", r"\1 pending", body)
    body = body.replace(
        "- Outcome: pending",
        "- Outcome: Update {{#each upgrades}}{{depName}} from {{currentValue}}"
        "{{#if currentDigest}} ({{currentDigest}}){{/if}} to {{newValue}}"
        "{{#if newDigest}} ({{newDigest}}){{/if}}{{#unless @last}}; {{/unless}}{{/each}}.",
    ).replace(
        "- Scope: pending",
        "- Scope: {{#each upgrades}}{{packageFile}}{{#unless @last}}; {{/unless}}{{/each}}.",
    )
    body = re.sub(r"\n{3,}", "\n\n", body).strip() + "\n"
    issues = _pull_request_body_issues(body, "draft")
    if issues:
        raise ProcessError("; ".join(issues))
    return {
        "$schema": "https://docs.renovatebot.com/renovate-schema.json",
        "description": "Pending process draft; generated from templates/PULL_REQUEST_TEMPLATE.md.",
        "prBodyTemplate": "{{{header}}}",
        "prHeader": body,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Reject a stale generated preset.")
    args = parser.parse_args()
    target = PROJECT_ROOT / "templates" / "renovate.json"
    template = target.with_name("PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
    expected = json.dumps(generate_preset(template), ensure_ascii=False, indent=2) + "\n"
    if args.check:
        if target.read_text(encoding="utf-8") != expected:
            raise ProcessError("templates/renovate.json is stale; run verification/generate_renovate_preset.py")
    else:
        target.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ProcessError) as error:
        print(f"Renovate preset generation failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
