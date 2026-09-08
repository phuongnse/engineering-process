#!/usr/bin/env python3
"""Generate the optional Renovate preset from the canonical public template."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engineering_process.contracts import (  # noqa: E402
    ProcessError,
    formatted_json_bytes,
    write_json_atomic,
)
from engineering_process.artifact_standards import resolve_standard  # noqa: E402
from engineering_process.distribution import distribution_root  # noqa: E402
from engineering_process.pr_description import render_renovate_preset, render_template  # noqa: E402


def generate_preset(template: str) -> dict[str, str]:
    standard = resolve_standard(None, distribution_root(), "pull-request")
    if template != render_template(standard):
        raise ProcessError("PR template differs from its packaged standard; regenerate it")
    return render_renovate_preset(standard)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Reject a stale generated preset.")
    args = parser.parse_args()
    target = PROJECT_ROOT / "templates" / "renovate.json"
    standard = resolve_standard(None, distribution_root(), "pull-request")
    template_path = target.with_name("PULL_REQUEST_TEMPLATE.md")
    template = render_template(standard)
    preset = generate_preset(template)
    if args.check:
        if template_path.read_bytes() != template.encode("utf-8"):
            raise ProcessError("templates/PULL_REQUEST_TEMPLATE.md is stale; run verification/generate_renovate_preset.py")
        if target.read_bytes() != formatted_json_bytes(preset):
            raise ProcessError("templates/renovate.json is stale; run verification/generate_renovate_preset.py")
    else:
        template_path.write_bytes(template.encode("utf-8"))
        write_json_atomic(target, preset)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ProcessError) as error:
        print(f"Renovate preset generation failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
